"""A scripted stand-in for the language model. Returns canned answers; no network."""

from packages.ai.model import AIError, ExplanationModel, ModelReply


class FakeExplanationModel(ExplanationModel):
    name = "fake-llm"

    def __init__(self, *replies: dict | AIError) -> None:
        self.replies = list(replies)
        self.calls: list[dict] = []

    async def generate(self, *, system: str, user: str, schema: dict) -> ModelReply:
        self.calls.append({"system": system, "user": user, "schema": schema})
        reply = self.replies.pop(0)
        if isinstance(reply, AIError):
            raise reply
        return ModelReply(data=reply, model="fake-model-1", request_id="req_fake")
