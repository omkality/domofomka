"""Текст и клавиатуры VK: чистые функции без сети и доступа к очереди."""

from src.vkbot.keyboard import INLINE_BUTTON_LIMIT, build_many_buttons, callback_button, serialize_inline_keyboard

ADDRESS_PAGE_SIZE = 5
# Место для навигации между подъездами и возврата к адресам.
SELECTED_ENTRANCES_PER_PAGE = INLINE_BUTTON_LIMIT - 3

def inline_keyboard(entrances: list[str]) -> str:
    return serialize_inline_keyboard(build_many_buttons(entrances))

def code_answer(result: dict, entrance: str) -> str:
    order = {"yaeda": 0, "delivery": 1, "oldcodes": 2}
    codes = sorted(result["data"][entrance], key=lambda item: order.get(item[1], 99))
    lines = []
    previous = None
    for code, kind in codes:
        if kind != previous:
            lines.extend(["", kind.upper() + ":"])
            previous = kind
        lines.append(code)
    return f"{result['address']}\nПодъезд {entrance}\n" + "\n".join(lines)

def building_answer(result: dict) -> str:
    data = result.get("data", {})
    if len(data) == 1:
        return code_answer(result, next(iter(data)))
    if not data:
        return result["address"] + "\nПо этому адресу кодов в нашей базе пока нет."
    return result["address"]

def address_choices(result: dict, page: int = 0) -> tuple[str, str]:
    choices = result["candidates"]
    size = ADDRESS_PAGE_SIZE
    start = page * size
    if page < 0 or start >= len(choices):
        raise ValueError("Invalid address page")
    subset = choices[start : start + size]
    labels = [c["address"] for c in subset]
    message = "Уточните адрес:\n" + "\n".join(f"{start + i + 1}. {label}" for i, label in enumerate(labels))
    message += "\nЕсли нужного варианта нет, пришлите адрес целиком одним сообщением: город, улицу и номер дома."

    rows = [[callback_button(str(start + i + 1), {"select_building": start + i}) for i in range(len(subset))]]
    navigation = []
    if page > 0:
        navigation.append(callback_button("Назад", {"choice_page": page - 1}))
    if start + size < len(choices):
        navigation.append(callback_button("Далее", {"choice_page": page + 1}))
    if navigation:
        rows.append(navigation)
    rows.append([callback_button("Ни один не подходит", {"dismiss_choices": True})])
    return message, serialize_inline_keyboard(rows)

def selected_keyboard(result: dict) -> str:
    """Подъезды выбранного адреса и возврат к сохранённому списку в одном сообщении."""
    entrances = list(result.get("data", {}))
    page = result["_address_choice"].get("entrance_page", 0)
    start = page * SELECTED_ENTRANCES_PER_PAGE
    if page < 0 or (start >= len(entrances) and (page or entrances)):
        raise ValueError("Invalid entrance page")
    subset = entrances[start : start + SELECTED_ENTRANCES_PER_PAGE] if len(entrances) > 1 else []
    rows = build_many_buttons(subset) if subset else []
    navigation = []
    if page:
        navigation.append(callback_button("Предыдущие подъезды", {"entrance_page": page - 1}))
    if start + SELECTED_ENTRANCES_PER_PAGE < len(entrances):
        navigation.append(callback_button("Следующие подъезды", {"entrance_page": page + 1}))
    if navigation:
        rows.append(navigation)
    rows.append([callback_button("Назад к адресам", {"return_to_choices": True})])
    return serialize_inline_keyboard(rows)
