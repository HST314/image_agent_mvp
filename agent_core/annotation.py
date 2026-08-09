"""Validated annotation composition for human image-to-image guidance."""
from __future__ import annotations
import io
from typing import Annotated, Literal
from PIL import Image, ImageColor, ImageDraw
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter

class Mark(BaseModel):
    model_config=ConfigDict(extra="forbid")
    kind: str; color: str = "#ff0000"; width: int = Field(3, ge=1, le=64)

class Rectangle(Mark):
    kind: Literal["rectangle"]; x: float=Field(ge=0,le=1); y: float=Field(ge=0,le=1); w: float=Field(gt=0,le=1); h: float=Field(gt=0,le=1)

class Stroke(Mark):
    kind: Literal["stroke"]; points: list[tuple[float,float]] = Field(min_length=2, max_length=5000)

Marks = TypeAdapter(list[Annotated[Rectangle | Stroke, Field(discriminator="kind")]])

def compose(image_bytes: bytes, marks: list[dict]) -> bytes:
    parsed=Marks.validate_python(marks)
    image=Image.open(io.BytesIO(image_bytes)).convert("RGBA"); draw=ImageDraw.Draw(image); width,height=image.size
    for mark in parsed:
        color=ImageColor.getrgb(mark.color)+(220,)
        if isinstance(mark, Rectangle):
            if mark.x+mark.w>1 or mark.y+mark.h>1: raise ValueError("ANNOTATION_OUT_OF_BOUNDS")
            draw.rectangle((mark.x*width,mark.y*height,(mark.x+mark.w)*width,(mark.y+mark.h)*height),outline=color,width=mark.width)
        else:
            if any(x<0 or x>1 or y<0 or y>1 for x,y in mark.points): raise ValueError("ANNOTATION_OUT_OF_BOUNDS")
            draw.line([(x*width,y*height) for x,y in mark.points],fill=color,width=mark.width,joint="curve")
    output=io.BytesIO(); image.save(output,"PNG"); return output.getvalue()
