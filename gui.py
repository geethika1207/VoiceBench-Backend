import asyncio
import gradio as gr

from app.services.ai_interview import ai_prompt
from app.services.tts_service import text_to_speech


async def start_interview(topic):

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

    audio = gr.Audio(
        label="AI Voice",
        autoplay=True
    )

    start_btn.click(
        fn=start_interview,
        inputs=topic,
        outputs=[
            question,
            message,
            audio
        ]
    )

demo.launch()