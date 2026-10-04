"""Operator-owned app configuration; business domains live in templates and adapters."""
import argparse
import asyncio
import json
import os
from pathlib import Path
from uuid import UUID, uuid4

from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool
from pydantic import Field

from .task_agents import Blueprint, digest, validate
from .tasks import Strict, TaskError


class Policy(Strict):
    planning_calls_per_day: int = Field(default=500, ge=0, le=100000)
    user_planning_calls_per_day: int = Field(default=50, ge=0, le=10000)
    max_plan_steps: int = Field(default=10, ge=1, le=30)


class Template(Strict):
    slug: str = Field(pattern=r'^[a-z][a-z0-9-]{0,79}$')
    version: int = Field(ge=1)
    blueprint: Blueprint
    enabled: bool = True


class AppManifest(Strict):
    app_id: UUID
    tools: list[str] = Field(max_length=100)
    policy: Policy = Field(default_factory=Policy)
    templates: list[Template] = Field(default_factory=list, max_length=100)


async def policy(conn, app_id):
    row = await (await conn.execute('SELECT * FROM platform_app_policies WHERE app_id=%s', (app_id,))).fetchone()
    return Policy(**{key: row[key] for key in Policy.model_fields}) if row else Policy()


async def configure(pool, registry, manifest):
    """Atomic replacement of app grants/policy; immutable, versioned built-in definitions."""
    if len(set(manifest.tools)) != len(manifest.tools) or not set(manifest.tools) <= registry.adapters.keys():
        raise TaskError('Manifest tools must be unique installed adapters')
    if len({(t.slug, t.version) for t in manifest.templates}) != len(manifest.templates):
        raise TaskError('Duplicate template versions')
    for template in manifest.templates:
        if len(template.blueprint.steps) > manifest.policy.max_plan_steps:
            raise TaskError('Template exceeds app step limit')
        validate(template.blueprint, registry, set(manifest.tools))
    async with pool.connection() as conn, conn.transaction():
        if not await (await conn.execute('SELECT id FROM platform_apps WHERE id=%s FOR UPDATE', (manifest.app_id,))).fetchone():
            raise TaskError('Provision the application before configuring it')
        await conn.execute('''INSERT INTO platform_app_policies(app_id,planning_calls_per_day,user_planning_calls_per_day,max_plan_steps)
            VALUES(%s,%s,%s,%s) ON CONFLICT(app_id) DO UPDATE SET planning_calls_per_day=excluded.planning_calls_per_day,
            user_planning_calls_per_day=excluded.user_planning_calls_per_day,max_plan_steps=excluded.max_plan_steps''',
            (manifest.app_id, manifest.policy.planning_calls_per_day, manifest.policy.user_planning_calls_per_day, manifest.policy.max_plan_steps))
        await conn.execute('DELETE FROM platform_tool_grants WHERE app_id=%s', (manifest.app_id,))
        for tool in manifest.tools:
            await conn.execute('INSERT INTO platform_tool_grants(app_id,tool) VALUES(%s,%s)', (manifest.app_id, tool))
        for template in manifest.templates:
            value = template.blueprint.model_dump()
            fingerprint = digest(value)
            prior = await (await conn.execute('SELECT definition_hash FROM platform_agent_templates WHERE app_id=%s AND slug=%s AND version=%s',
                (manifest.app_id, template.slug, template.version))).fetchone()
            if prior and prior['definition_hash'] != fingerprint:
                raise TaskError('Template versions are immutable; publish a new version')
            await conn.execute('''INSERT INTO platform_agent_templates(id,app_id,slug,version,blueprint,definition_hash,enabled)
                VALUES(%s,%s,%s,%s,%s,%s,%s) ON CONFLICT(app_id,slug,version) DO UPDATE SET enabled=excluded.enabled''',
                (uuid4(), manifest.app_id, template.slug, template.version, Jsonb(value), fingerprint, template.enabled))


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('manifest', type=Path)
    args = parser.parse_args()
    manifest = AppManifest.model_validate_json(args.manifest.read_text())
    from .decision_tool import default_registry
    from .research_tool import install_research_tool
    async with AsyncConnectionPool(os.environ['AI_PLATFORM_DATABASE_URL'], open=False, kwargs={'row_factory': dict_row}) as pool:
        registry = default_registry()
        install_research_tool(registry, pool)
        from .knowledge import KnowledgeAnswer
        registry.adapters['knowledge.answer'] = KnowledgeAnswer(pool)
        await configure(pool, registry, manifest)
    print(json.dumps({'app_id': str(manifest.app_id), 'templates': len(manifest.templates), 'configured': True}))


if __name__ == '__main__':
    asyncio.run(main())
