"""Построение клавиатур VK с проверкой ограничений платформы."""

import json

from vk_api.keyboard import VkKeyboard

INLINE_BUTTON_LIMIT = 10
BUTTONS_PER_ROW = 5


def callback_button(label: str, payload: dict) -> dict:
    """Создать callback-кнопку с заданным текстом и полезной нагрузкой."""
    return {
        "action": {"type": "callback", "label": label, "payload": json.dumps(payload)},
        "color": "secondary",
    }


def serialize_inline_keyboard(rows: list[list[dict]]) -> str:
    """Проверить лимиты и сериализовать inline-клавиатуру VK."""
    if sum(len(row) for row in rows) > INLINE_BUTTON_LIMIT or any(
        not row or len(row) > BUTTONS_PER_ROW for row in rows
    ):
        raise ValueError("Inline keyboard exceeds the application's button budget")
    return json.dumps({"inline": True, "buttons": rows}, ensure_ascii=False)


def build_one_button(entrance: str, entrances: list[str]) -> dict:
    """Создать кнопку выбора одного подъезда."""
    return {
        "action": {
            "type": "callback",
            "label": entrance,
            "payload": json.dumps(
                {
                    "entrance": entrance,
                    "ent_slice": entrances,
                }
            ),
        },
    }


def build_many_buttons(entrances: list[str]) -> list[list[dict]]:
    """Разложить кнопки подъездов по допустимым строкам клавиатуры."""
    buttons = []
    ent_normalized = []

    rows = (len(entrances) - 1) // BUTTONS_PER_ROW + 1
    for j in range(rows):
        ent_normalized.append(entrances[j * BUTTONS_PER_ROW : (j + 1) * BUTTONS_PER_ROW])

    for i_row in range(len(ent_normalized)):
        btn_arr = []
        for j_ent in range(len(ent_normalized[i_row])):
            btn_arr.append(build_one_button(ent_normalized[i_row][j_ent], entrances))
        buttons.append(btn_arr)

    return buttons


def get_location_keyboard() -> str:
    """Подготовить клавиатуру отправки геолокации."""
    keyboard = VkKeyboard()
    keyboard.add_location_button()
    return keyboard.get_keyboard()
