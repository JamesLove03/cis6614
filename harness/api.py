import os

from fastapi import FastAPI
from pydantic import BaseModel, Field

from .detector import Detector, chat_reply

app = FastAPI(title="Company chatbot simulation")


class Message(BaseModel):
    message: str = Field(min_length=1, max_length=32000)


@app.post("/chat")
def chat(body: Message):
    detector = Detector()
    result = detector.detect(body.message, execute=os.getenv("JEV_EXECUTE") == "1")
    return chat_reply(body.message, result)
