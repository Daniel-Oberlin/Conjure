"""Typed world-document and patch schema (architecture.md §4, §5).

Phase-0 scope: enough structure to render and live-edit a scene. Components and
environment are intentionally open dicts for now; we'll firm them into typed
component models as the renderer grows.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal, Optional, Union

from pydantic import BaseModel, Field

Vec3 = tuple[float, float, float]


# ---------------------------------------------------------------- world model

class Transform(BaseModel):
    position: Vec3 = (0.0, 0.0, 0.0)
    rotation: Vec3 = (0.0, 0.0, 0.0)  # Euler degrees (A-Frame convention)
    scale: Vec3 = (1.0, 1.0, 1.0)


class Entity(BaseModel):
    id: str
    parent: Optional[str] = None  # entity id, or an anchor id (anchor-relative placement)
    transform: Transform = Field(default_factory=Transform)
    components: dict[str, Any] = Field(default_factory=dict)  # name -> A-Frame component value
    behaviors: list[dict[str, Any]] = Field(default_factory=list)  # BehaviorRef[] (see §9)
    meta: dict[str, Any] = Field(default_factory=dict)


class Budget(BaseModel):
    maxTris: int = 500_000
    maxDrawCalls: int = 200
    texMemMB: int = 256
    targetHz: int = 90


class World(BaseModel):
    id: str
    name: str = ""
    description: str = ""  # indexed for semantic recall later
    tags: list[str] = Field(default_factory=list)
    rev: int = 0  # bumped on every applied patch
    budget: Budget = Field(default_factory=Budget)
    environment: dict[str, Any] = Field(default_factory=dict)
    anchors: list[dict[str, Any]] = Field(default_factory=list)
    entities: list[Entity] = Field(default_factory=list)
    connections: list[dict[str, Any]] = Field(default_factory=list)


# ---------------------------------------------------------------- patch protocol

class AddOp(BaseModel):
    op: Literal["add"]
    entity: Entity


class UpdateOp(BaseModel):
    op: Literal["update"]
    id: str
    set: dict[str, Any]  # dotted path -> value, e.g. {"components.light.intensity": 3.0}


class RemoveOp(BaseModel):
    op: Literal["remove"]
    id: str


class EnvOp(BaseModel):
    op: Literal["env"]
    set: dict[str, Any]  # dotted path -> value on world.environment


Op = Annotated[Union[AddOp, UpdateOp, RemoveOp, EnvOp], Field(discriminator="op")]


class Patch(BaseModel):
    rev: Optional[int] = None  # server-assigned on apply
    origin: str = "user"  # director | behavior:<id> | module:<name> | user:<id>
    ops: list[Op]
    inverse: Optional[list[dict[str, Any]]] = None  # server-computed, for undo
