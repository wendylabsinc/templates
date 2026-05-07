"""
Albert Agent — processes chat messages and manages persistent camera watch tasks.

When a user says something like "alert me if you see a fire", the agent:
1. Classifies the message as a watch request
2. Creates a WatchTask with the condition
3. Polls the camera at intervals, feeding frames to Gemma 4 vision
4. If the condition is detected, sends an alert back through the store
"""

import threading
import time
import uuid
from dataclasses import dataclass, field

from camera import Camera
from llm import llm
from store import LoroStore

ALBERT_USER_ID = "albert"

CLASSIFY_SYSTEM_PROMPT = """\
You are a message classifier. Given a user message, determine if it is:
1. WATCH — the user wants you to continuously monitor the camera for a condition (e.g., "alert me if you see fire", "let me know when someone walks by")
2. DESCRIBE — the user wants you to describe what the camera currently sees (e.g., "what do you see?", "describe the scene")
3. CHAT — a normal conversational message that does not involve the camera

Reply with ONLY one word: WATCH, DESCRIBE, or CHAT"""

WATCH_EXTRACT_PROMPT = """\
Extract the monitoring condition from this message. Reply with ONLY the condition as a short phrase.
For example:
- "alert me if you see a fire" → "fire or flames visible"
- "tell me when a person appears" → "a person is visible"
- "notify me if the door opens" → "a door is open"

Message: {message}"""


@dataclass
class WatchTask:
    task_id: str
    conversation_id: str
    condition: str
    interval_seconds: float = 5.0
    active: bool = True


class Agent:
    def __init__(self, store: LoroStore, camera: Camera):
        self.store = store
        self.camera = camera
        self._watch_tasks: dict[str, WatchTask] = {}
        self._watch_thread: threading.Thread | None = None
        self._running = False

    def start(self):
        self._running = True
        self._watch_thread = threading.Thread(target=self._watch_loop, daemon=True)
        self._watch_thread.start()

    def stop(self):
        self._running = False
        close = getattr(self.camera, "close", None)
        if callable(close):
            close()

    def process_message(
        self, conversation_id: str, user_message: str
    ) -> dict:
        """Process an incoming message and return a response dict with reply + optional title."""
        # Classify intent
        intent = self._classify(user_message)

        if intent == "WATCH":
            return self._handle_watch(conversation_id, user_message)
        elif intent == "DESCRIBE":
            return self._handle_describe(conversation_id, user_message)
        else:
            return self._handle_chat(conversation_id, user_message)

    def _classify(self, message: str) -> str:
        result = llm.chat(
            [
                {"role": "system", "content": CLASSIFY_SYSTEM_PROMPT},
                {"role": "user", "content": message},
            ],
            max_tokens=5,
        )
        word = result.strip().upper()
        if word in ("WATCH", "DESCRIBE", "CHAT"):
            return word
        return "CHAT"

    def _handle_watch(self, conversation_id: str, message: str) -> dict:
        # Extract the condition
        condition = llm.chat(
            [
                {"role": "user", "content": WATCH_EXTRACT_PROMPT.format(message=message)},
            ],
            max_tokens=30,
        ).strip()

        task = WatchTask(
            task_id=str(uuid.uuid4()),
            conversation_id=conversation_id,
            condition=condition,
        )
        self._watch_tasks[task.task_id] = task

        reply_body = f'Got it — I\'m now watching for: "{condition}". I\'ll alert you when I see it.'
        reply = self.store.add_reply(
            conversation_id=conversation_id, user_id=ALBERT_USER_ID, body=reply_body
        )
        return {"reply": reply, "generatedTitle": None}

    def _handle_describe(self, conversation_id: str, message: str) -> dict:
        frame = self.camera.capture_jpeg_base64()
        if frame is None:
            reply_body = "I can't access the camera right now."
        else:
            reply_body = llm.chat_with_image(
                "Describe what you see in this image in detail.", frame
            )

        reply = self.store.add_reply(
            conversation_id=conversation_id, user_id=ALBERT_USER_ID, body=reply_body
        )
        return {"reply": reply, "generatedTitle": None}

    def _handle_chat(self, conversation_id: str, message: str) -> dict:
        # Build conversation history
        history = self.store.list_messages(conversation_id)
        messages = [
            {"role": "system", "content": "You are Albert, a helpful AI assistant with camera vision capabilities. You can watch for things and describe what you see."}
        ]
        for msg in history:
            role = "user" if msg["userId"] != ALBERT_USER_ID else "assistant"
            messages.append({"role": role, "content": msg["body"]})

        reply_body = llm.chat(messages)
        reply = self.store.add_reply(
            conversation_id=conversation_id, user_id=ALBERT_USER_ID, body=reply_body
        )
        return {"reply": reply, "generatedTitle": None}

    # -- Background watch loop --

    def _watch_loop(self):
        while self._running:
            active_tasks = [t for t in self._watch_tasks.values() if t.active]
            if not active_tasks:
                time.sleep(1)
                continue

            frame = self.camera.capture_jpeg_base64()
            if frame is None:
                time.sleep(1)
                continue

            for task in active_tasks:
                self._check_condition(task, frame)

            # Sleep for the shortest interval
            interval = min(t.interval_seconds for t in active_tasks)
            time.sleep(interval)

    def _check_condition(self, task: WatchTask, frame_base64: str):
        prompt = (
            f'Look at this image carefully. Is this condition true: "{task.condition}"?\n'
            f"Reply with ONLY 'YES' or 'NO' on the first line, then a brief explanation."
        )
        result = llm.chat_with_image(prompt, frame_base64, max_tokens=60)

        first_line = result.strip().split("\n")[0].upper()
        if "YES" in first_line:
            # Condition detected — send alert
            explanation = result.strip()
            alert_body = f'🚨 Alert: I detected "{task.condition}"!\n\n{explanation}'
            self.store.add_reply(
                conversation_id=task.conversation_id,
                user_id=ALBERT_USER_ID,
                body=alert_body,
            )
            # Keep watching (user can stop via chat)
