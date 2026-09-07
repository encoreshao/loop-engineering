#!/usr/bin/env python3
"""LoopDefinition dataclasses + YAML loader - see
docs/superpowers/specs/2026-09-06-loop-runtime-foundation-design.md.
Validation lives here (not gated behind a future `loop validate` CLI)
because `LoopRuntime` needs a validated definition regardless of whether
a CLI exists yet."""
from dataclasses import dataclass, field

import yaml

_STOP_CONDITION_DEFAULTS = {
    "max_iterations": 3,
    "max_runtime_minutes": 30,
    "max_cost_usd": 5,
    "no_progress_iterations": 2,
}

_RETRY_DEFAULTS = {
    "enabled": True,
    "max_attempts": 2,
}


@dataclass
class TriggerConfig:
    type: str
    schedule: str | None = None


@dataclass
class GoalConfig:
    type: str


@dataclass
class AgentConfig:
    provider: str | None = None
    model: str | None = None


@dataclass
class ContextConfig:
    sources: list = field(default_factory=list)


@dataclass
class VerificationConfig:
    required: list = field(default_factory=list)


@dataclass
class StopConditions:
    max_iterations: int = _STOP_CONDITION_DEFAULTS["max_iterations"]
    max_runtime_minutes: int = _STOP_CONDITION_DEFAULTS["max_runtime_minutes"]
    max_cost_usd: float = _STOP_CONDITION_DEFAULTS["max_cost_usd"]
    no_progress_iterations: int = _STOP_CONDITION_DEFAULTS["no_progress_iterations"]


@dataclass
class RetryConfig:
    enabled: bool = _RETRY_DEFAULTS["enabled"]
    max_attempts: int = _RETRY_DEFAULTS["max_attempts"]


@dataclass
class LoopDefinition:
    name: str
    version: int
    trigger: TriggerConfig
    goal: GoalConfig
    agent: AgentConfig = field(default_factory=AgentConfig)
    context: ContextConfig = field(default_factory=ContextConfig)
    actions: list = field(default_factory=list)
    verification: VerificationConfig = field(default_factory=VerificationConfig)
    stop_conditions: StopConditions = field(default_factory=StopConditions)
    human_gates: list = field(default_factory=list)
    retry: RetryConfig = field(default_factory=RetryConfig)

    @staticmethod
    def from_dict(data):
        for required_key in ("name", "version", "trigger", "goal"):
            if required_key not in data:
                raise ValueError(f"LoopDefinition: missing required field '{required_key}'")

        trigger_data = data["trigger"]
        if "type" not in trigger_data:
            raise ValueError("LoopDefinition: missing required field 'trigger.type'")

        goal_data = data["goal"]
        if "type" not in goal_data:
            raise ValueError("LoopDefinition: missing required field 'goal.type'")

        agent_data = data.get("agent", {})
        context_data = data.get("context", {})
        verification_data = data.get("verification", {})
        stop_conditions_data = data.get("stop_conditions", {})
        retry_data = data.get("retry", {})

        return LoopDefinition(
            name=data["name"],
            version=data["version"],
            trigger=TriggerConfig(type=trigger_data["type"], schedule=trigger_data.get("schedule")),
            goal=GoalConfig(type=goal_data["type"]),
            agent=AgentConfig(provider=agent_data.get("provider"), model=agent_data.get("model")),
            context=ContextConfig(sources=context_data.get("sources", [])),
            actions=data.get("actions", []),
            verification=VerificationConfig(required=verification_data.get("required", [])),
            stop_conditions=StopConditions(
                max_iterations=stop_conditions_data.get("max_iterations", _STOP_CONDITION_DEFAULTS["max_iterations"]),
                max_runtime_minutes=stop_conditions_data.get(
                    "max_runtime_minutes", _STOP_CONDITION_DEFAULTS["max_runtime_minutes"]
                ),
                max_cost_usd=stop_conditions_data.get("max_cost_usd", _STOP_CONDITION_DEFAULTS["max_cost_usd"]),
                no_progress_iterations=stop_conditions_data.get(
                    "no_progress_iterations", _STOP_CONDITION_DEFAULTS["no_progress_iterations"]
                ),
            ),
            human_gates=data.get("human_gates", []),
            retry=RetryConfig(
                enabled=retry_data.get("enabled", _RETRY_DEFAULTS["enabled"]),
                max_attempts=retry_data.get("max_attempts", _RETRY_DEFAULTS["max_attempts"]),
            ),
        )

    @staticmethod
    def from_yaml(path):
        with open(path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f)
        return LoopDefinition.from_dict(data)
