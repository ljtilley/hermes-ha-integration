"""Conversation entity for Hermes Agent."""

from __future__ import annotations

import logging
import re
import time
import uuid
from collections import OrderedDict
from collections.abc import AsyncIterator
from typing import Any

from homeassistant.components import conversation as ha_conversation
from homeassistant.components.conversation import (
    AbstractConversationAgent,
    ConversationInput,
    ConversationResult,
    MATCH_ALL,
)
from homeassistant.components.homeassistant.exposed_entities import async_should_expose
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import area_registry as ar
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import intent, template
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

try:
    from homeassistant.components.conversation.chat_log import (
        ChatLog,
        async_get_chat_log,
    )
    from homeassistant.helpers.chat_session import async_get_chat_session
except ImportError:  # pragma: no cover - compatibility with older Home Assistant
    ChatLog = Any  # type: ignore[misc,assignment]
    async_get_chat_log = None
    async_get_chat_session = None

from .api import HermesApiClient, HermesApiError
from .compat import entry_value
from .const import (
    CONF_ALWAYS_SPEAK_FALLBACK,
    CONF_AUTO_FOLLOW_UP,
    CONF_CONTEXT_MAX_CHARS,
    CONF_ENABLE_CONTINUED_CONVERSATION,
    CONF_ENABLE_SESSION_REUSE,
    CONF_EXPOSE_DEVICE_CONTEXT,
    CONF_FALLBACK_MEDIA_PLAYER,
    CONF_FALLBACK_TTS_ENGINE,
    CONF_INCLUDE_EXPOSED_ENTITIES,
    CONF_PROMPT,
    CONF_SESSION_TIMEOUT_SECONDS,
    DEFAULT_ALWAYS_SPEAK_FALLBACK,
    DEFAULT_AUTO_FOLLOW_UP,
    DEFAULT_CONTEXT_MAX_CHARS,
    DEFAULT_ENABLE_CONTINUED_CONVERSATION,
    DEFAULT_ENABLE_SESSION_REUSE,
    DEFAULT_EXPOSE_DEVICE_CONTEXT,
    DEFAULT_FALLBACK_MEDIA_PLAYER,
    DEFAULT_FALLBACK_TTS_ENGINE,
    DEFAULT_INCLUDE_EXPOSED_ENTITIES,
    DEFAULT_MAX_HISTORY_MESSAGES,
    DEFAULT_PROMPT,
    DEFAULT_SESSION_TIMEOUT_SECONDS,
    DOMAIN,
    LEGACY_CONF_INSTRUCTIONS,
)
from .tool_trace_filter import (
    append_tool_trace_prompt,
    sanitize_response_text,
    should_hide_tool_traces,
)

_LOGGER = logging.getLogger(__name__)
_MAX_CACHED_CONVERSATIONS = 50
_MAX_SESSION_MAP_ENTRIES = 100
_HAS_CHAT_LOG_API = (
    async_get_chat_log is not None and async_get_chat_session is not None
)
_QUESTION_MARKERS = ("?", "？")
_TRAILING_FOLLOW_UP_MAX_CHARS = 120
_TRAILING_FOLLOW_UP_MAX_WORDS = 20
_TRAILING_FOLLOW_UP_MAX_SENTENCE_ENDERS = 1
_TRAILING_SENTENCE_ENDERS = ".!！;；"
_TRAILING_CLOSERS = "\"'”’)]}»"
_AUTO_FOLLOW_UP_PROMPT = (
    "When voice auto follow-up is active and you want the user to reply, "
    "give any needed answer first and end with one short, direct question as "
    "the final sentence. Do not add any words after the question mark."
)


def _sanitize_text_for_speech(text: str) -> str:
    """Convert markdown-ish assistant output into plain speech-friendly text."""
    if not text:
        return text
    cleaned = text.replace("\r\n", "\n")
    cleaned = re.sub(r"```(?:[\w+-]+)?\n?(.*?)```", r"\1", cleaned, flags=re.DOTALL)
    cleaned = re.sub(r"`([^`]+)`", r"\1", cleaned)
    cleaned = re.sub(r"!\[([^\]]*)\]\([^\)]+\)", r"\1", cleaned)
    cleaned = re.sub(r"\[([^\]]+)\]\([^\)]+\)", r"\1", cleaned)
    cleaned = re.sub(r"^#{1,6}\s*", "", cleaned, flags=re.MULTILINE)
    cleaned = re.sub(r"^>+\s*", "", cleaned, flags=re.MULTILINE)
    cleaned = re.sub(r"^\s*[-*+]\s+", "", cleaned, flags=re.MULTILINE)
    cleaned = re.sub(r"^\s*\d+[.)]\s+", "", cleaned, flags=re.MULTILINE)
    cleaned = re.sub(r"(\*\*|__)(.*?)\1", r"\2", cleaned)
    cleaned = re.sub(r"(?<!\*)\*(?!\s)(.*?)(?<!\s)\*(?!\*)", r"\1", cleaned)
    cleaned = re.sub(r"(?<!_)_(?!\s)(.*?)(?<!\s)_(?!_)", r"\1", cleaned)
    cleaned = re.sub(r"~~(.*?)~~", r"\1", cleaned)
    cleaned = re.sub(r"\[(.*?)\]\[[^\]]*\]", r"\1", cleaned)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    cleaned = re.sub(r"[ \t]+", " ", cleaned)
    return cleaned.strip()


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the Hermes conversation entity."""
    data = hass.data[DOMAIN][entry.entry_id]
    client: HermesApiClient = data["client"]
    session_map: dict[str, dict[str, Any]] = data["sessions"]
    async_add_entities([HermesConversationEntity(hass, entry, client, session_map)])


class HermesConversationEntity(
    ha_conversation.ConversationEntity,
    AbstractConversationAgent,
):
    """Hermes Agent conversation entity for Home Assistant."""

    _attr_supports_streaming = True

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        client: HermesApiClient,
        session_map: dict[str, dict[str, Any]] | None = None,
    ) -> None:
        """Initialise the conversation agent."""
        self.hass = hass
        self.entry = entry
        self.client = client
        self.session_map: dict[str, dict[str, Any]] = (
            session_map if session_map is not None else {}
        )
        self._attr_unique_id = entry.entry_id
        self._attr_name = entry.title or "Hermes Agent"
        self._attr_supported_features = (
            ha_conversation.ConversationEntityFeature.CONTROL
        )
        # conversation_id -> list of {"role": ..., "content": ...}
        self._history: OrderedDict[str, list[dict[str, str]]] = OrderedDict()

    @property
    def supported_languages(self) -> list[str] | str:
        """Return supported languages (all — the LLM handles it)."""
        return MATCH_ALL

    async def async_added_to_hass(self) -> None:
        """Register the entity as both conversation entity and legacy agent."""
        await super().async_added_to_hass()
        ha_conversation.async_set_agent(self.hass, self.entry, self)

    async def async_will_remove_from_hass(self) -> None:
        """Unregister the legacy agent alias when removing the entity."""
        ha_conversation.async_unset_agent(self.hass, self.entry)
        await super().async_will_remove_from_hass()

    async def async_process(
        self, user_input: ConversationInput
    ) -> ConversationResult:
        """Process a conversation turn."""
        try:
            if _HAS_CHAT_LOG_API:
                return await self._async_process_modern(user_input)
            return await self._async_process_legacy(user_input)
        except Exception:
            _LOGGER.exception("Unexpected error in async_process")
            intent_response = intent.IntentResponse(language=user_input.language)
            intent_response.async_set_error(
                intent.IntentResponseErrorCode.UNKNOWN,
                "An internal error occurred. Check the logs.",
            )
            return self._build_conversation_result(
                intent_response,
                user_input.conversation_id or "default",
            )

    async def _async_process_modern(
        self, user_input: ConversationInput
    ) -> ConversationResult:
        """Process a conversation turn using Home Assistant chat sessions."""
        assert async_get_chat_log is not None
        assert async_get_chat_session is not None

        with (
            async_get_chat_session(self.hass, user_input.conversation_id) as session,
            async_get_chat_log(self.hass, session, user_input) as chat_log,
        ):
            return await self._async_handle_message(user_input, chat_log)

    async def _async_handle_message(
        self,
        user_input: ConversationInput,
        chat_log: ChatLog,
    ) -> ConversationResult:
        """Handle a conversation turn using the modern chat log API."""
        options = self.entry.options
        user_name = await self._get_user_name(user_input)
        system_prompt = self._build_system_prompt(options, user_input, user_name)
        messages = self._build_messages_from_chat_log(chat_log, system_prompt)

        session_reuse = self._session_reuse_enabled()
        session_key = (
            self._build_session_key(user_input, chat_log.conversation_id)
            if session_reuse
            else None
        )
        session_id = (
            self._get_active_session_id(session_key) if session_key else None
        )

        # Streaming is bypassed only when we need the full text upfront for
        # post-processing: tool-trace filtering or fallback TTS dispatch.
        # Session reuse is orthogonal — the API client tracks the session id
        # from response headers whether the body is streamed or not.
        use_streaming = not (
            should_hide_tool_traces(options)
            or self._always_speak_fallback_enabled()
        )

        response_text = ""
        try:
            if use_streaming:
                async for content in chat_log.async_add_delta_content_stream(
                    self.entity_id or self.entry.entry_id,
                    self._async_stream_assistant_response(messages, session_id),
                ):
                    if (
                        getattr(content, "role", None) == "assistant"
                        and getattr(content, "content", None) is not None
                    ):
                        response_text += content.content
            else:
                response_text = await self._get_full_response(messages, session_id)
        except HermesApiError as err:
            _LOGGER.error("Hermes API error: %s", err)
            intent_response = intent.IntentResponse(language=user_input.language)
            intent_response.async_set_error(
                intent.IntentResponseErrorCode.UNKNOWN,
                f"Error communicating with Hermes Agent: {err}",
            )
            return self._build_conversation_result(
                intent_response,
                chat_log.conversation_id,
            )

        if session_key:
            self._remember_session(session_key, self.client.last_session_id)

        spoken_text = response_text
        if should_hide_tool_traces(options):
            spoken_text = sanitize_response_text(spoken_text)
        spoken_text = _sanitize_text_for_speech(spoken_text)

        intent_response = intent.IntentResponse(language=user_input.language)
        intent_response.async_set_speech(spoken_text)
        await self._async_speak_fallback(spoken_text, user_input)

        continue_conversation = self._compute_continue_conversation(
            options, response_text
        )

        return self._build_conversation_result(
            intent_response,
            chat_log.conversation_id,
            continue_conversation=continue_conversation,
        )

    async def _async_process_legacy(
        self, user_input: ConversationInput
    ) -> ConversationResult:
        """Process a conversation turn on older Home Assistant versions."""
        options = self.entry.options
        user_name = await self._get_user_name(user_input)
        system_prompt = self._build_system_prompt(options, user_input, user_name)

        conv_id = user_input.conversation_id or str(uuid.uuid4())

        session_reuse = self._session_reuse_enabled()
        session_key = (
            self._build_session_key(user_input, conv_id) if session_reuse else None
        )
        session_id = (
            self._get_active_session_id(session_key) if session_key else None
        )

        if session_reuse:
            messages: list[dict[str, str]] = []
            if system_prompt:
                messages.append({"role": "system", "content": system_prompt})
            messages.append({"role": "user", "content": user_input.text})
        else:
            history = self._history.setdefault(conv_id, [])
            messages = self._build_messages_from_history(history, system_prompt)
            messages.append({"role": "user", "content": user_input.text})

        try:
            if should_hide_tool_traces(options):
                response_text = await self._get_full_response(messages, session_id)
            else:
                response_text = await self._get_response(messages, session_id)
        except HermesApiError as err:
            _LOGGER.error("Hermes API error: %s", err)
            intent_response = intent.IntentResponse(language=user_input.language)
            intent_response.async_set_error(
                intent.IntentResponseErrorCode.UNKNOWN,
                f"Error communicating with Hermes Agent: {err}",
            )
            return self._build_conversation_result(intent_response, conv_id)

        if session_key:
            self._remember_session(session_key, self.client.last_session_id)

        # Bug fix vs upstream PR #2: store RAW response_text in history,
        # not the markdown-sanitized spoken_text. Sanitizing for TTS shouldn't
        # corrupt the multi-turn context Hermes sees on the next turn.
        if not session_reuse:
            history.append({"role": "user", "content": user_input.text})
            history.append({"role": "assistant", "content": response_text})
            self._history.move_to_end(conv_id)
            self._trim_legacy_history(history)
            self._trim_cached_conversations()

        spoken_text = response_text
        if should_hide_tool_traces(options):
            spoken_text = sanitize_response_text(spoken_text)
        spoken_text = _sanitize_text_for_speech(spoken_text)

        intent_response = intent.IntentResponse(language=user_input.language)
        intent_response.async_set_speech(spoken_text)
        await self._async_speak_fallback(spoken_text, user_input)

        continue_conversation = self._compute_continue_conversation(
            options, response_text
        )

        return self._build_conversation_result(
            intent_response,
            conv_id,
            continue_conversation=continue_conversation,
        )

    # Sentence-ending patterns checked after each delta append.
    # Ordered from strongest to weakest to avoid false splits.
    _SENTENCE_ENDS: tuple[tuple[str, ...], ...] = (
        (". ", "! ", "? "),           # Period/exclaim/question + space
        (".\n", "!\n", "?\n"),         # Period/exclaim/question + newline
        ("... ", "...\n"),             # Ellipsis
        (": ", ":\n"),                # Colon (precedes list/explanation)
        (";\n",),                      # Semicolon + newline (list items)
    )
    _MAX_BUFFER_CHARS: int = 200       # Flush if no sentence end after this many chars

    @staticmethod
    def _extract_first_sentence(buffer: str) -> tuple[str, str]:
        """Extract the first complete sentence from the buffer.

        Returns (sentence, remaining_buffer).  If no sentence end is found
        and the buffer is under MAX_BUFFER_CHARS, returns ("", buffer).
        If the buffer exceeds MAX_BUFFER_CHARS, flushes the whole buffer
        as one chunk.
        """
        # Max-buffer guard: flush everything if we hit the limit
        if len(buffer) >= HermesConversationEntity._MAX_BUFFER_CHARS:
            return buffer, ""

        for end_group in HermesConversationEntity._SENTENCE_ENDS:
            for end in end_group:
                pos = buffer.find(end)
                if pos != -1:
                    sentence = buffer[: pos + len(end)]
                    remaining = buffer[pos + len(end):]
                    # If the sentence is just the end marker (empty content),
                    # skip it and try the remaining buffer.
                    stripped = sentence.strip()
                    if not stripped or stripped in (".", "!", "?", "...", ":", ";"):
                        # Recurse into the remaining buffer to find a real sentence
                        inner_sentence, inner_remaining = (
                            HermesConversationEntity._extract_first_sentence(
                                remaining
                            )
                        )
                        if inner_sentence:
                            return sentence + inner_sentence, inner_remaining
                        return sentence, remaining
                    return sentence, remaining

        return "", buffer

    async def _async_stream_assistant_response(
        self,
        messages: list[dict[str, str]],
        session_id: str | None = None,
    ) -> AsyncIterator[dict[str, str]]:
        """Yield sentence-buffered assistant deltas for Home Assistant's chat log.

        Accumulates streaming tokens into a buffer and yields one complete
        sentence at a time, each wrapped with its own assistant role marker.
        This encourages the HA pipeline to treat each sentence as a separate
        utterance rather than one continuous TTS stream.
        """
        received_delta = False
        buffer = ""

        try:
            async for delta in self.client.async_stream_message(
                messages, session_id=session_id
            ):
                if not delta:
                    continue
                received_delta = True

                buffer += delta
                sentence, buffer = self._extract_first_sentence(buffer)
                if sentence:
                    sentence = sentence.lstrip()
                    if sentence:
                        yield {"role": "assistant", "content": sentence}

        except HermesApiError as err:
            if received_delta:
                if buffer.strip():
                    yield {"role": "assistant", "content": buffer.strip()}
                _LOGGER.warning(
                    "Streaming interrupted after partial response, keeping partial text: %s",
                    err,
                )
                return

            _LOGGER.debug(
                "Streaming failed before first token, falling back to non-streaming: %s",
                err,
            )

        if received_delta:
            if buffer.strip():
                yield {"role": "assistant", "content": buffer.strip()}
            return

        result = await self.client.async_send_message(
            messages, session_id=session_id
        )
        if result.text:
            # Stream the fallback text through the same sentence-buffer
            buffer = result.text
            while buffer:
                sentence, buffer = self._extract_first_sentence(buffer)
                if sentence:
                    sentence = sentence.lstrip()
                    if sentence:
                        yield {"role": "assistant", "content": sentence}
                else:
                    if buffer.strip():
                        yield {"role": "assistant", "content": buffer.strip()}
                    break

    async def _get_response(
        self,
        messages: list[dict[str, str]],
        session_id: str | None = None,
    ) -> str:
        """Get a response from the API, trying streaming first."""
        try:
            chunks: list[str] = []
            async for delta in self.client.async_stream_message(
                messages, session_id=session_id
            ):
                chunks.append(delta)
            if chunks:
                return "".join(chunks)
        except HermesApiError:
            _LOGGER.debug("Streaming failed, falling back to non-streaming")

        result = await self.client.async_send_message(
            messages, session_id=session_id
        )
        return result.text

    async def _get_full_response(
        self,
        messages: list[dict[str, str]],
        session_id: str | None = None,
    ) -> str:
        """Get a complete response from the API without streaming."""
        result = await self.client.async_send_message(
            messages, session_id=session_id
        )
        return result.text

    def _build_messages_from_chat_log(
        self,
        chat_log: ChatLog,
        system_prompt: str,
    ) -> list[dict[str, str]]:
        """Build Hermes messages from a Home Assistant chat log."""
        history_messages: list[dict[str, str]] = []

        for content in chat_log.content:
            role = getattr(content, "role", None)
            message_text = getattr(content, "content", None)

            # The current system prompt is rebuilt for each turn, so we avoid
            # replaying older system entries from Home Assistant's chat log.
            if role not in ("user", "assistant") or not message_text:
                continue

            history_messages.append({"role": role, "content": message_text})

        return self._build_messages_from_history(history_messages, system_prompt)

    def _build_messages_from_history(
        self,
        history: list[dict[str, str]],
        system_prompt: str,
    ) -> list[dict[str, str]]:
        """Build Hermes messages from text history."""
        messages: list[dict[str, str]] = []

        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})

        if history:
            messages.extend(history[-DEFAULT_MAX_HISTORY_MESSAGES:])

        return messages

    def _trim_legacy_history(self, history: list[dict[str, str]]) -> None:
        """Trim legacy in-memory history."""
        while len(history) > DEFAULT_MAX_HISTORY_MESSAGES:
            history.pop(0)
            if history and history[0]["role"] == "assistant":
                history.pop(0)

    def _trim_cached_conversations(self) -> None:
        """Evict oldest legacy conversations if too many are cached."""
        while len(self._history) > _MAX_CACHED_CONVERSATIONS:
            self._history.popitem(last=False)

    async def _get_user_name(self, user_input: ConversationInput) -> str:
        """Resolve the display name of the user from HA auth."""
        try:
            context = getattr(user_input, "context", None)
            if context is None:
                return "the user"
            user_id = getattr(context, "user_id", None)
            if not user_id:
                return "the user"
            user = await self.hass.auth.async_get_user(user_id)
            if user and user.name:
                return user.name
        except Exception:
            _LOGGER.debug("Could not resolve username", exc_info=True)
        return "the user"

    def _build_system_prompt(
        self,
        options: dict[str, Any],
        user_input: ConversationInput,
        user_name: str,
    ) -> str:
        """Build the full system prompt: template + extra + origin + tool-trace nudge."""
        system_prompt = self._render_system_prompt(options, user_name)

        extra_system_prompt = getattr(user_input, "extra_system_prompt", None)
        if extra_system_prompt:
            system_prompt = (
                f"{system_prompt}\n\n{extra_system_prompt}"
                if system_prompt
                else extra_system_prompt
            )

        if self._device_context_enabled():
            context_lines = self._build_origin_context(user_input)
            if context_lines:
                origin_block = "Origin context:\n" + "\n".join(
                    f"- {line}" for line in context_lines
                )
                system_prompt = (
                    f"{system_prompt}\n\n{origin_block}"
                    if system_prompt
                    else origin_block
                )

        return append_tool_trace_prompt(options, system_prompt)

    def _render_system_prompt(self, options: dict[str, Any], user_name: str) -> str:
        """Render the configured system prompt template with HA context."""
        prompt_template = entry_value(
            self.entry,
            CONF_PROMPT,
            DEFAULT_PROMPT,
            legacy_keys=(LEGACY_CONF_INSTRUCTIONS,),
        )
        if not prompt_template:
            return self._append_auto_follow_up_prompt(options, "")

        variables: dict[str, Any] = {
            "ha_name": self.hass.config.location_name,
            "user_name": user_name,
        }

        include_entities = entry_value(
            self.entry,
            CONF_INCLUDE_EXPOSED_ENTITIES,
            DEFAULT_INCLUDE_EXPOSED_ENTITIES,
        )
        if include_entities:
            variables["exposed_entities"] = self._get_exposed_entities()
        else:
            variables["exposed_entities"] = []

        try:
            tpl = template.Template(prompt_template, self.hass)
            rendered_prompt = tpl.async_render(variables)
        except template.TemplateError as err:
            _LOGGER.warning("System prompt template error: %s", err)
            rendered_prompt = prompt_template

        return self._append_auto_follow_up_prompt(options, rendered_prompt)

    def _append_auto_follow_up_prompt(
        self,
        options: dict[str, Any],
        system_prompt: str,
    ) -> str:
        """Append extra guidance that makes spoken follow-up turns cleaner."""
        if not options.get(CONF_AUTO_FOLLOW_UP, DEFAULT_AUTO_FOLLOW_UP):
            return system_prompt

        if system_prompt:
            return f"{system_prompt}\n\n{_AUTO_FOLLOW_UP_PROMPT}"

        return _AUTO_FOLLOW_UP_PROMPT

    def _get_exposed_entities(self) -> list[dict[str, str]]:
        """Get a list of entities exposed to the conversation agent."""
        max_chars = entry_value(
            self.entry,
            CONF_CONTEXT_MAX_CHARS,
            DEFAULT_CONTEXT_MAX_CHARS,
        )
        entities: list[dict[str, str]] = []
        total_chars = 0

        for state in self.hass.states.async_all():
            try:
                if not async_should_expose(
                    self.hass, "conversation", state.entity_id
                ):
                    continue
            except Exception:
                continue

            entity_info = {
                "entity_id": state.entity_id,
                "name": state.attributes.get("friendly_name", state.entity_id),
                "state": str(state.state),
            }

            line = (
                f"- {entity_info['entity_id']} "
                f"({entity_info['name']}): {entity_info['state']}"
            )
            total_chars += len(line) + 1

            if total_chars > max_chars:
                break

            entities.append(entity_info)

        return entities

    # ----- continue_conversation logic ---------------------------------

    def _compute_continue_conversation(
        self, options: dict[str, Any], response_text: str
    ) -> bool:
        """True if either continued-conversation or auto-follow-up applies."""
        if self._continued_conversation_enabled():
            return True
        return self._should_continue_conversation(options, response_text)

    def _should_continue_conversation(
        self, options: dict[str, Any], response_text: str
    ) -> bool:
        """Auto follow-up: continue when the assistant ended with a question."""
        if not options.get(CONF_AUTO_FOLLOW_UP, DEFAULT_AUTO_FOLLOW_UP):
            return False

        stripped_text = response_text.strip().rstrip(_TRAILING_CLOSERS)
        if not stripped_text:
            return False

        if stripped_text.endswith(_QUESTION_MARKERS):
            return True

        last_question_pos = max(
            stripped_text.rfind(marker) for marker in _QUESTION_MARKERS
        )
        if last_question_pos == -1:
            return False

        trailing_text = stripped_text[last_question_pos + 1 :].strip()
        if not trailing_text:
            return True

        trailing_words = trailing_text.split()
        trailing_sentence_enders = sum(
            trailing_text.count(marker) for marker in _TRAILING_SENTENCE_ENDERS
        )

        return (
            len(trailing_text) <= _TRAILING_FOLLOW_UP_MAX_CHARS
            and len(trailing_words) <= _TRAILING_FOLLOW_UP_MAX_WORDS
            and trailing_sentence_enders <= _TRAILING_FOLLOW_UP_MAX_SENTENCE_ENDERS
        )

    # ----- session reuse helpers ---------------------------------------

    def _continued_conversation_enabled(self) -> bool:
        return bool(
            entry_value(
                self.entry,
                CONF_ENABLE_CONTINUED_CONVERSATION,
                DEFAULT_ENABLE_CONTINUED_CONVERSATION,
            )
        )

    def _session_reuse_enabled(self) -> bool:
        return bool(
            entry_value(
                self.entry,
                CONF_ENABLE_SESSION_REUSE,
                DEFAULT_ENABLE_SESSION_REUSE,
            )
        )

    def _session_timeout_seconds(self) -> int:
        try:
            return max(
                0,
                int(
                    entry_value(
                        self.entry,
                        CONF_SESSION_TIMEOUT_SECONDS,
                        DEFAULT_SESSION_TIMEOUT_SECONDS,
                    )
                ),
            )
        except (TypeError, ValueError):
            return DEFAULT_SESSION_TIMEOUT_SECONDS

    def _device_context_enabled(self) -> bool:
        return bool(
            entry_value(
                self.entry,
                CONF_EXPOSE_DEVICE_CONTEXT,
                DEFAULT_EXPOSE_DEVICE_CONTEXT,
            )
        )

    def _always_speak_fallback_enabled(self) -> bool:
        return bool(
            entry_value(
                self.entry,
                CONF_ALWAYS_SPEAK_FALLBACK,
                DEFAULT_ALWAYS_SPEAK_FALLBACK,
            )
        )

    def _build_session_key(
        self, user_input: ConversationInput, conversation_id: str
    ) -> str:
        device_id = getattr(user_input, "device_id", None)
        satellite_id = getattr(user_input, "satellite_id", None)
        if device_id:
            return f"device:{device_id}"
        if satellite_id:
            return f"satellite:{satellite_id}"
        return f"conversation:{conversation_id}"

    def _get_active_session_id(self, session_key: str | None) -> str | None:
        if not session_key:
            return None

        record = self.session_map.get(session_key)
        if not record:
            return None

        session_id = record.get("session_id")
        last_used_at = float(record.get("last_used_at", 0) or 0)
        timeout_seconds = self._session_timeout_seconds()
        if timeout_seconds and (time.time() - last_used_at) > timeout_seconds:
            self.session_map.pop(session_key, None)
            return None

        if isinstance(session_id, str) and session_id.strip():
            return session_id
        return None

    def _remember_session(
        self, session_key: str, session_id: str | None
    ) -> None:
        if not session_id:
            self.session_map.pop(session_key, None)
            return

        self.session_map[session_key] = {
            "session_id": session_id,
            "last_used_at": time.time(),
        }
        # Bound the session map (Devin-flagged from PR #2 review): drop the
        # least-recently-used entry once we exceed the cap.
        if len(self.session_map) > _MAX_SESSION_MAP_ENTRIES:
            stale_key = min(
                self.session_map,
                key=lambda k: self.session_map[k].get("last_used_at", 0),
            )
            self.session_map.pop(stale_key, None)

    # ----- origin context ----------------------------------------------

    def _build_origin_context(
        self, user_input: ConversationInput
    ) -> list[str]:
        lines: list[str] = []
        language = getattr(user_input, "language", None)
        device_id = getattr(user_input, "device_id", None)
        satellite_id = getattr(user_input, "satellite_id", None)

        if language:
            lines.append(f"Language: {language}")
        if device_id:
            lines.extend(self._describe_device(device_id))
        if satellite_id:
            lines.extend(self._describe_satellite(satellite_id))
        return lines

    def _describe_device(self, device_id: str) -> list[str]:
        device_reg = dr.async_get(self.hass)
        area_reg = ar.async_get(self.hass)
        device = device_reg.async_get(device_id)
        if not device:
            return [f"Home Assistant device_id: {device_id}"]

        lines = [
            f"Origin device: {device.name_by_user or device.name or device_id}"
        ]
        if device.area_id:
            area = area_reg.async_get_area(device.area_id)
            if area:
                lines.append(f"Origin area: {area.name}")
                area_entities = self._area_exposed_entities(device.area_id)
                if area_entities:
                    lines.append("Exposed entities in this area:")
                    lines.extend(f"  {line}" for line in area_entities)
        return lines

    def _area_exposed_entities(self, area_id: str) -> list[str]:
        """List exposed conversation entities in the given area, with state."""
        ent_reg = er.async_get(self.hass)
        device_reg = dr.async_get(self.hass)
        max_entities = 40

        seen: set[str] = set()
        results: list[str] = []
        for entry in ent_reg.entities.values():
            if entry.entity_id in seen:
                continue
            in_area = entry.area_id == area_id
            if not in_area and entry.device_id:
                device = device_reg.async_get(entry.device_id)
                if device and device.area_id == area_id:
                    in_area = True
            if not in_area:
                continue
            if not async_should_expose(self.hass, "conversation", entry.entity_id):
                continue

            state = self.hass.states.get(entry.entity_id)
            if state is None:
                continue

            friendly = state.attributes.get("friendly_name") or entry.name or entry.entity_id
            results.append(f"- {entry.entity_id} ({friendly}): {state.state}")
            seen.add(entry.entity_id)
            if len(results) >= max_entities:
                break

        results.sort()
        return results

    def _describe_satellite(self, satellite_id: str) -> list[str]:
        state = (
            self.hass.states.get(satellite_id) if "." in satellite_id else None
        )
        if not state:
            return [f"Assist satellite: {satellite_id}"]
        friendly_name = state.attributes.get("friendly_name", satellite_id)
        return [f"Assist satellite: {friendly_name} ({satellite_id})"]

    # ----- fallback TTS ------------------------------------------------

    async def _async_speak_fallback(
        self, text: str, user_input: ConversationInput
    ) -> None:
        if not text.strip():
            return

        if not (
            getattr(user_input, "device_id", None)
            or getattr(user_input, "satellite_id", None)
        ):
            return

        if not self._always_speak_fallback_enabled():
            return

        media_player_entity = entry_value(
            self.entry,
            CONF_FALLBACK_MEDIA_PLAYER,
            DEFAULT_FALLBACK_MEDIA_PLAYER,
        )
        tts_entity = entry_value(
            self.entry,
            CONF_FALLBACK_TTS_ENGINE,
            DEFAULT_FALLBACK_TTS_ENGINE,
        )
        if not media_player_entity or not tts_entity:
            return

        service_data = {
            "entity_id": tts_entity,
            "media_player_entity_id": media_player_entity,
            "message": text,
            "cache": True,
        }

        language = getattr(user_input, "language", None)
        if language:
            service_data["language"] = language

        try:
            await self.hass.services.async_call(
                "tts",
                "speak",
                service_data,
                blocking=True,
            )
        except Exception as err:
            _LOGGER.warning("Fallback TTS failed: %s", err)

    # ----- result builder ---------------------------------------------

    def _build_conversation_result(
        self,
        intent_response: intent.IntentResponse,
        conversation_id: str,
        *,
        continue_conversation: bool = False,
    ) -> ConversationResult:
        """Build a conversation result, preserving compatibility with older HA."""
        try:
            return ConversationResult(
                response=intent_response,
                conversation_id=conversation_id,
                continue_conversation=continue_conversation,
            )
        except TypeError:
            return ConversationResult(
                response=intent_response,
                conversation_id=conversation_id,
            )
