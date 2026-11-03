"""Exercise the actual Runtime authority method against changing room policy."""
import ast
import asyncio
from pathlib import Path
from types import SimpleNamespace

source = Path(__file__).parent.parent / "custom_components/intercom_m3_poc/__init__.py"
tree = ast.parse(source.read_text(encoding="utf-8"))
runtime_node = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "Runtime")
method = next(node for node in runtime_node.body if isinstance(node, ast.AsyncFunctionDef) and node.name == "authorize_owner")
module = ast.Module(body=[method], type_ignores=[])
class Refused(ValueError):
    pass
scope = {"Refused": Refused, "_b64": lambda value: "call"}
exec(compile(module, str(source), "exec"), scope)
authorize_owner = scope["authorize_owner"]


async def run_case(change):
    entered, release = asyncio.Event(), asyncio.Event()
    policy = {"allowed": True, "dnd": False}
    owner = SimpleNamespace(call_id=bytes(16), principal=SimpleNamespace(user_id="user", refresh_token_id="token"))
    class Manager:
        closed = False
        async def wait_history_start(self, call):
            entered.set()
            await release.wait()
        def permitted(self, user, room):
            return policy["allowed"]
        def dnd(self, room):
            return policy["dnd"]
    runtime = SimpleNamespace(manager=Manager(), session=SimpleNamespace(owner=owner), accepting=True,
                              device_id="room", _expire_preparing=lambda: None,
                              _token_user=lambda user, token: None)
    task = asyncio.create_task(authorize_owner(runtime, owner))
    await entered.wait()
    if change == "dnd":
        policy["dnd"] = True
    elif change == "acl":
        policy["allowed"] = False
    elif change == "owner":
        runtime.session.owner = None
    release.set()
    assert await task is (change is None), change


async def main():
    for change in (None, "dnd", "acl", "owner"):
        await run_case(change)
    print("PASS: actual authority method rejects DND, ACL and owner changes during its await")


asyncio.run(main())


