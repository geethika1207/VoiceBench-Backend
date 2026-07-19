import asyncio
import gradio as gr

from app.services.ai_interview import ai_prompt
from app.services.tts_service import text_to_speech

import websockets
import numpy as np

async def submit_answer(audio):

    sample_rate, samples = audio

    uri = "ws://127.0.0.1:8000/ws/interview/1"

    async with websockets.connect(uri) as ws:

        await ws.send(samples.astype(np.int16).tobytes())

        response = await ws.recv()

    return response

async def start_interview(topic, audio):

    result = ai_prompt(topic)

    if result.get("error"):
        return "", result["error"], None

    audio_path = await text_to_speech(result["question"])

    return (
        result["question"],
        result["message"],
        audio_path
    )


with gr.Blocks(title="VoiceBench V2") as demo:

    gr.Markdown("# 🎤 VoiceBench V2")

    topic = gr.Textbox(
        label="Interview Topic",
        placeholder="Example: FastAPI"
    )

    start_btn = gr.Button("Start Interview")

    message = gr.Textbox(
        label="Welcome Message",
        interactive=False
    )

    question = gr.Textbox(
        label="First Question",
        interactive=False
    )

    audio_input = gr.Audio(
        label="Your Answer",
        sources=["microphone"],
        streaming=True,
        type="numpy"
    )

    audio = gr.Audio(
        label="AI Voice",
        autoplay=True
    )

    submit_btn = gr.Button("Submit Answer")

    start_btn.click(
        fn=start_interview,
        inputs=[topic, audio_input],
        outputs=[
            question,
            message,
            audio
        ]
    )

    submit_btn.click(
        fn=submit_answer,
        inputs=[audio_input],
        outputs=[message]
    )
    
demo.launch()