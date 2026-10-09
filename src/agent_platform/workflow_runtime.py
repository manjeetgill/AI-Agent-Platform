"""Sequential typed workflows; references select prior results, never executable code."""
import json

from pydantic import Field, model_validator

from .tasks import Action, StepInput, Strict, TaskError


class Binding(Strict):
    step: int = Field(ge=0, le=7)
    pointer: str = Field(pattern=r'^(?:/(?:[^~]|~[01])*)?$', max_length=300)


class Node(Strict):
    tool: str = Field(min_length=1, max_length=100)
    version: str = Field(min_length=1, max_length=40)
    arguments: dict = Field(default_factory=dict)
    bindings: dict[str, Binding] = Field(default_factory=dict, max_length=20)


class Workflow(Strict):
    steps: list[Node] = Field(min_length=1, max_length=8)

    @model_validator(mode='after')
    def valid_references(self):
        for position, step in enumerate(self.steps):
            if step.tool.startswith('runtime.'):
                raise ValueError('Nested orchestration is not supported')
            for name, binding in step.bindings.items():
                if not name or len(name)>100 or name in step.arguments or binding.step >= position:
                    raise ValueError('Bindings require distinct top-level arguments and earlier completed steps')
                if len(binding.pointer.split('/'))>11:
                    raise ValueError('Result reference is too deep')
        return self


class FlowResult(Strict):
    done: bool
    completed_steps: int = Field(ge=0, le=8)
    next_tool: str | None = None
    next_version: str | None = None
    next_arguments: dict = Field(default_factory=dict)
    explanation: str


def select(value, pointer):
    if pointer == '':
        return value
    for part in pointer.split('/')[1:]:
        part = part.replace('~1', '/').replace('~0', '~')
        if isinstance(value, dict) and part in value:
            value = value[part]
        elif isinstance(value, list) and part.isascii() and part.isdigit() and (part == '0' or not part.startswith('0')) and int(part)<len(value):
            value = value[int(part)]
        else:
            raise TaskError('Required earlier result is missing; workflow cannot continue')
    return value


class WorkflowRuntime:
    version, risk, timeout = '1', 'READ', 10
    input_model, output_model = Workflow, FlowResult
    description = ('Execute at most eight version-pinned tools in sequence. Bind top-level arguments '
                   'from earlier verified step results using JSON pointers. Sending still requires exact approval.')

    def __init__(self, pool, registry):
        self.pool, self.registry = pool, registry

    def prepare(self, arguments):
        flow = Workflow.model_validate(arguments, strict=True)
        for node in flow.steps:
            adapter = self.registry.adapters.get(node.tool)
            if adapter is None or adapter.version != node.version or hasattr(adapter, 'continuation'):
                raise TaskError('Workflow tool version is unavailable or embeds another continuation')
            if not node.bindings:
                self.registry.prepare(StepInput(tool=node.tool, arguments=node.arguments))
        return Action('runtime.workflow', '1', flow.model_dump(),
                      f'Coordinate {len(flow.steps)} typed steps; review each resolved sending action before execution.', 'READ')

    async def execute(self, action, *, task_id, authorized, **context):
        if not await authorized():
            raise TaskError('Execution authority revoked')
        flow = Workflow.model_validate(action.arguments)
        from .app_catalogue import policy
        async with self.pool.connection() as conn:
            task = await (await conn.execute('SELECT source_app_id,current_step FROM chore_tasks WHERE id=%s', (task_id,))).fetchone()
            if not task or not task['source_app_id']:
                raise TaskError('Workflows require an application identity')
            if 2*len(flow.steps)+1 > (await policy(conn, task['source_app_id'])).max_plan_steps:
                raise TaskError('Workflow exceeds application step budget')
            grants = {r['tool'] for r in await (await conn.execute('SELECT tool FROM platform_tool_grants WHERE app_id=%s', (task['source_app_id'],))).fetchall()}
            if any(n.tool not in grants for n in flow.steps):
                raise TaskError('Workflow requires an ungranted domain tool')
            rows = await (await conn.execute('''SELECT tool,tool_version,result,status FROM chore_steps
                WHERE task_id=%s AND position<%s AND tool<>'runtime.workflow' ORDER BY position''',
                (task_id, task['current_step']))).fetchall()
        for index, row in enumerate(rows):
            if index>=len(flow.steps) or row['status']!='DONE' or (row['tool'],row['tool_version'])!=(flow.steps[index].tool,flow.steps[index].version):
                raise TaskError('Workflow history does not match its pinned definition')
        if len(rows)==len(flow.steps):
            return FlowResult(done=True, completed_steps=len(rows), explanation='All workflow steps completed; use the saved domain results.').model_dump()
        node = flow.steps[len(rows)]
        arguments = dict(node.arguments)
        for name, binding in node.bindings.items():
            arguments[name] = select(rows[binding.step]['result'], binding.pointer)
        if len(json.dumps(arguments, allow_nan=False).encode())>16000:
            raise TaskError('Resolved arguments exceed the execution budget')
        prepared = self.registry.prepare(StepInput(tool=node.tool, arguments=arguments))
        if prepared.version!=node.version:
            raise TaskError('Tool changed since the workflow was saved')
        return FlowResult(done=False, completed_steps=len(rows), next_tool=node.tool,
                          next_version=node.version, next_arguments=prepared.arguments,
                          explanation='Next action prepared from verified earlier results.').model_dump()

    def continuation(self, action, result):
        outcome = FlowResult.model_validate(result)
        if outcome.done:
            return []
        next_action = self.registry.prepare(StepInput(tool=outcome.next_tool, arguments=outcome.next_arguments))
        if next_action.version != outcome.next_version:
            raise TaskError('Workflow tool version changed')
        return [next_action, action]

    async def reconcile(self, action, **context):
        return None


def install(registry, pool):
    registry.adapters['runtime.workflow'] = WorkflowRuntime(pool, registry)
