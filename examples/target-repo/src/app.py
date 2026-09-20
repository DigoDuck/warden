"""A deliberately small FastAPI service.

This is the repository Warden's agents work on: they read it, patch it and open pull
requests against it. It is kept small so that a capability eval can assert on its whole
behaviour, and so that a task spec fits in one sentence.
"""

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

app = FastAPI(title="Widget Service", version="0.1.0")


class Widget(BaseModel):
    id: int
    name: str = Field(min_length=1, max_length=64)
    price_cents: int = Field(ge=0)


class WidgetCreate(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    price_cents: int = Field(ge=0)


# In-memory on purpose: this service exists to be edited, not to keep data.
_WIDGETS: dict[int, Widget] = {
    1: Widget(id=1, name="bolt", price_cents=250),
    2: Widget(id=2, name="washer", price_cents=80),
}
_NEXT_ID = 3


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/widgets")
def list_widgets() -> list[Widget]:
    return list(_WIDGETS.values())


@app.get("/widgets/{widget_id}")
def get_widget(widget_id: int) -> Widget:
    widget = _WIDGETS.get(widget_id)
    if widget is None:
        raise HTTPException(status_code=404, detail="widget not found")
    return widget


@app.post("/widgets", status_code=201)
def create_widget(payload: WidgetCreate) -> Widget:
    global _NEXT_ID
    widget = Widget(id=_NEXT_ID, name=payload.name, price_cents=payload.price_cents)
    _WIDGETS[widget.id] = widget
    _NEXT_ID += 1
    return widget
