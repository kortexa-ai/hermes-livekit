"""Native Hermes text frames, independent of the streaming audio lifetime."""


class NativeTranscriptMixin:
    SUPPORTS_NATIVE_STREAMING = True
    SUPPORTS_MESSAGE_EDITING = False

    def supports_native_streaming(self, chat_type=None, metadata=None):
        return True

    async def send_stream_frame(
        self, content, *, chat_id, turn_id, finalize=False, reply_to=None,
    ):
        endpoint = self._tts_endpoint(chat_id)
        if endpoint is None:
            # A closed transport is not a reason to replay a stale final send.
            return True
        protocol = endpoint[1]
        await protocol.stream_transcript(content, turn_id=turn_id, finalize=finalize)
        return True
