"""Cooperate with Private Companion's per-event delivery callbacks.

Its decorators replace event.send and send_streaming. The captured callbacks
remain inside its policies and are reused when it restores the event. Observe
there instead of attributing a shared send counter to one particular image.
"""

COMPANION_CONTEXT = "_companion_v2_core_reply_context"


class DeliveryUnconfirmed(RuntimeError):
    """An interceptor returned without confirming this image's delivery."""


def companion_context(event):
    state = event.get_extra(COMPANION_CONTEXT)
    return state if isinstance(state, dict) else None


def companion_block_reason(event):
    state = companion_context(event)
    if state is None:
        return ""
    participation = state.get("group_participation")
    if isinstance(participation, dict) and participation.get("mode") == "skip":
        return "当前群聊回复已选择旁听"
    if state.get("denial_notified"):
        return "当前回复未获发送授权"
    if state.get("send_unknown"):
        return "此前发送结果不明，本轮不再补发"
    segmentation = state.get("_reply_segmentation") or state.get("_group_segmentation")
    if isinstance(segmentation, dict) and segmentation.get("stopped"):
        return "当前分段发送已停止"
    return ""


def bind_companion_sender(event, name, wrap):
    """Wrap a captured callback on this event only; never replace its policies.

    This is a compatibility contract with Private Companion 7.0.x, not an
    AstrBot public API. Unknown layouts are left untouched and cannot provide
    delivery evidence. Keep every private field name in this module.
    """
    state = companion_context(event)
    if state is None or not callable(state.get("_send_wrapper")):
        return False
    key = "_original_" + name
    original = state.get(key)
    if not callable(original):
        return False
    wrapped = wrap(original)
    state[key] = wrapped
    if getattr(event, name, None) is original:
        setattr(event, name, wrapped)
    return True


async def authorize_companion_image(event):
    """Recheck live permission when Companion restored its unguarded sender."""
    state = companion_context(event)
    if state is None or state.get("delivery_guard_active"):
        return
    authorize = state.get("_authorize_delivery")
    if not callable(authorize):
        raise DeliveryUnconfirmed("陪伴插件发送授权接口已变化，无法核验图片送达")
    await authorize()
