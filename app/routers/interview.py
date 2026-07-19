from fastapi import APIRouter, Depends, status, HTTPException, WebSocket, WebSocketDisconnect
from sqlalchemy.orm import Session
from ..db.database import SessionLocal, get_db
from ..core.security import get_current_user
from ..schemas import interview
from ..db import models
from ..services.ai_interview import ai_prompt
from ..services.ai_interview_analysis import ai_analysis_prompt
from ..services.tts_service import text_to_speech
from ..services.stt_service import SpeechToText
from ..services.vad_service import VoiceActivityDetector
from ..services.buffer_service import AudioBuffer
import os
import asyncio
import time

os.makedirs("temp", exist_ok=True)

stt = SpeechToText()
buffer = AudioBuffer()


router = APIRouter()

@router.post("/interview/start")
async def interview_topic(selected_topic : interview.InterviewTopic, db:Session=Depends(get_db), current_user=Depends(get_current_user)):
    try:
        result = ai_prompt(selected_topic.concept)
        error = result.get("error")
        
        if error:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=error)
        
        title = result.get("title")
        message = result.get("message")
        question = result.get("question")

        speech_text = question

        audio_filename = await text_to_speech(speech_text)

        print(audio_filename)

        new_interview = models.Interview(
                            user_id = current_user.id,
                            title = title,
                            topics = selected_topic.concept,
                            status = "active"
        )

        db.add(new_interview)
        db.commit()
        db.refresh(new_interview)
        print("Interview created:", new_interview.id)

        first_question = models.Response(
                            interview_id = new_interview.id,
                            question = question
        )

        db.add(first_question)
        db.commit()
        db.refresh(first_question)

    except HTTPException:
        raise
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=f"Internal server error : {str(e)}")
    
    return {
        "id": new_interview.id,
        "title": title,
        "topics": selected_topic.concept,
        "topic_created_at": new_interview.created_at,
        "message": message,
        "question": question,
        "audio_url": f"/audio/{audio_filename}",
        "question_created_time": first_question.created_at
    }


@router.websocket("/ws/interview/{id}")
async def interview_socket(
    websocket: WebSocket,
    id: int,
    db: Session = Depends(get_db)
):

    await websocket.accept()

    print("Hello")

    interview = (
        db.query(models.Interview)
        .filter(models.Interview.id == id)
        .first()
    )

    if not interview:
        await websocket.send_json(
            {"error": "Interview not found"}
        )
        await websocket.close()
        return                                        # return says "stop executing this endpoint."


    recent_response = db.query(models.Response).filter(
         models.Response.interview_id == interview.id
     ).order_by(
         models.Response.created_at.desc()
     ).first()


    if not recent_response:
        await websocket.send_json(
            {"error": "Interview not found"}
        )
        await websocket.close()
        return

    current_difficulty = recent_response.difficulty or "Beginner"


    # ---------------------------------
    # Services

    stt = SpeechToText()

    vad = VoiceActivityDetector()


    await stt.connect()


    # ---------------------------------
    # Queues

    deepgram_queue = asyncio.Queue()

    vad_queue = asyncio.Queue()

    buffer_queue = asyncio.Queue()

    vad_finished = asyncio.Event()


    # =================================
    # Deepgram Worker

    async def deepgram_worker():

        while True:

            chunk = await deepgram_queue.get()

            try:

                await stt.send_chunk(chunk)

            except Exception as e:

                print("DEEPGRAM WORKER ERROR:", e)

            finally:

                deepgram_queue.task_done()



    # =================================
    # VAD Worker

    async def vad_worker():

        while True:

            chunk = await vad_queue.get()

            try:

                if vad.is_speech_finished(chunk):           # statement under if executes only if the speech completed by the user

                    vad_finished.set()                      # VAD signal ON

            except Exception as e:

                print("VAD WORKER ERROR:", e)

            finally:

                vad_queue.task_done()



    # =================================
    # Buffer Worker

    async def buffer_worker():

        while True:

            chunk = await buffer_queue.get()

            try:

                buffer.add_chunk(chunk)

            except Exception as e:

                print("BUFFER WORKER ERROR:", e)

            finally:

                buffer_queue.task_done()



    # ---------------------------------
    # Start Workers

    deepgram_task = asyncio.create_task(
        deepgram_worker()
    )

    vad_task = asyncio.create_task(
        vad_worker()
    )

    buffer_task = asyncio.create_task(
        buffer_worker()
    )


    idle_count = 0

    pipeline_start = None

    try:

        while True:

            # Wait until frontend finishes playing AI audio
            message = await websocket.receive()

            if message["type"] == "websocket.receive":

                if "text" in message:

                    if message["text"] != "audio_finished":
                        continue

                elif "bytes" in message:
                    continue

            # NOW start waiting for user speech
            while True:

                try:

                    chunk = await asyncio.wait_for(
                        websocket.receive_bytes(),
                        timeout=5
                    )

                    if pipeline_start is None:
                        pipeline_start = time.perf_counter()

                    idle_count = 0

                    await deepgram_queue.put(chunk)

                    await vad_queue.put(chunk)

                    await buffer_queue.put(chunk)

                except asyncio.TimeoutError:

                    if not vad_finished.is_set():

                        idle_count += 1

                        if idle_count == 1:

                            await websocket.send_text(
                                "Are you still there?"
                            )

                            continue

                        else:

                            await websocket.send_text(
                                "Moving to next question..."
                            )

                            vad_finished.set()

                if not vad_finished.is_set():
                    continue

                # Reset for next question

                vad_finished.clear()

                idle_count = 0

                await deepgram_queue.join()

                await vad_queue.join()

                stt_start = time.perf_counter()

                transcript = await stt.get_transcript()

                stt_latency = (time.perf_counter() - stt_start) * 1000

                await stt.reset_transcript()

                vad.reset()

                try:

                    llm_start = time.perf_counter()

                    result = await asyncio.wait_for(
                        ai_analysis_prompt(
                            recent_response.question,
                            transcript,
                            interview.topics,
                            current_difficulty
                        ),
                        timeout=20
                    )

                    llm_latency = (time.perf_counter() - llm_start) * 1000

                except asyncio.TimeoutError:

                    await websocket.send_text(
                        "AI server is busy. Please wait..."
                    )

                    continue

                # ==============================
                # Save

                # Update current question
                recent_response.answer = transcript
                recent_response.marks = result.get("score")
                recent_response.evaluation = result.get("evaluation")
                recent_response.difficulty = current_difficulty

                #Insert next question

                next_response = models.Response(
                    interview_id=id,
                    question=result["next_question"],
                    difficulty=result["difficulty"]
                )

                db_start = time.perf_counter()

                db.add(next_response)
                db.commit()
                db.refresh(next_response)

                db_latency = (time.perf_counter() - db_start) * 1000

                recent_response = next_response

                current_difficulty = next_response.difficulty

                # ==============================
                # TTS

                tts_start = time.perf_counter()

                filename = await text_to_speech(
                    result["next_question"]
                )

                tts_latency = (time.perf_counter() - tts_start) * 1000

                send_start = time.perf_counter()

                print("Sending next question...")
                print("Next question sent successfully")

                await websocket.send_json({

                    "question": result["next_question"],

                    "audio_url": f"/audio/{filename}"

                })

                send_latency = (time.perf_counter() - send_start) * 1000

                total_latency = (time.perf_counter() - pipeline_start) * 1000

                print("\n==============================")
                print(" VoiceBench Latency Report")
                print("==============================")
                print(f"STT        : {stt_latency:.2f} ms")
                print(f"LLM        : {llm_latency:.2f} ms")
                print(f"Database   : {db_latency:.2f} ms")
                print(f"TTS        : {tts_latency:.2f} ms")
                print(f"WebSocket  : {send_latency:.2f} ms")
                print("------------------------------")
                print(f"TOTAL      : {total_latency:.2f} ms")
                print("==============================\n")

                pipeline_start = None

                break  # go back to outer loop and wait for the next "audio_finished"

    except WebSocketDisconnect:

        print("Client disconnected")

    except Exception as e:
        print("SERVER ERROR:", e)
        raise

    finally:

        print("Closing websocket...")
        deepgram_task.cancel()
        vad_task.cancel()
        buffer_task.cancel()

        await stt.close()

        await websocket.close()
        

# ENDPOINT FOR AUDIO FILE UPLOAD FOR HTTP REQUEST WE DONT HAVE TO WRITE CONNECT AND CLOSE...

# @router.post("/interview/{id}/answer")
# async def answer_analysis(
#     id: int,
#     audio: UploadFile = File(...),  
#     db: Session = Depends(get_db),
#     current_user=Depends(get_current_user)
# ):
    
#     interview = db.query(models.Interview).filter(
#         models.Interview.id == id,
#         models.Interview.user_id == current_user.id
#     ).first()

#     if not interview:
#         raise HTTPException(
#             status_code=status.HTTP_404_NOT_FOUND,
#             detail="Interview not found."
#         )

#     recent_response = db.query(models.Response).filter(
#         models.Response.interview_id == interview.id
#     ).order_by(
#         models.Response.created_at.desc()
#     ).first()

#     if not recent_response:
#         raise HTTPException(
#             status_code=status.HTTP_404_NOT_FOUND,
#             detail="No interview question found."
#         )

#     current_difficulty = recent_response.difficulty or "Beginner"

#     try:

#         file_path = f"temp/{uuid.uuid4()}.mp3"

#         with open(file_path, "wb") as f:
#             f.write(await audio.read())

#         transcript = await speech_to_text(file_path)
#         print(transcript)

#         result = ai_analysis_prompt(
#             recent_response.question,
#             transcript,
#             interview.topics,
#             current_difficulty
#         )

#         print(result.get("score"))
#         print(result.get("evaluation"))

#         # Update current question
#         recent_response.answer = transcript
#         recent_response.marks = result.get("score")
#         recent_response.evaluation = result.get("evaluation")
#         recent_response.difficulty = current_difficulty

#         # Insert next question
#         next_response = models.Response(
#             interview_id=id,
#             question=result["next_question"],
#             difficulty=result["difficulty"]
#         )

#         db.add(next_response)
#         db.commit()
#         db.refresh(next_response)

#         audio_filename = await text_to_speech(next_response.question)

#         return {
#             "interview_id" : id,
#             "question": next_response.question,
#             "difficulty": next_response.difficulty,
#             "audio_url": f"/audio/{audio_filename}",
#             "created_at": next_response.created_at
#         }

#     except Exception as e:
#         db.rollback()
#         traceback.print_exc()   
#         raise HTTPException(
#             status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
#             detail=f"INTERNAL SERVER ERROR : {str(e)}"
#         )