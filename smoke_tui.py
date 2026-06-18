import asyncio, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent))
import tui, cc

async def main():
    app = tui.CCApp()
    async with app.run_test() as pilot:
        await pilot.pause()
        tree = app.query_one("#tree")
        nproj = len(tree.root.children)
        assert nproj >= 1, "no project nodes"

        # free-create epic on a NON-jira project (jira projects use pick/create-in-jira mode)
        s = cc.load_state()
        free = next((n for n, p in s["projects"].items() if not p.get("jira", {}).get("token")), None)
        if not free:
            print("TUI smoke OK: %d project(s); all jira-enabled (epic modal = jira mode)" % nproj)
            return
        app.push_screen(tui.NewEpicScreen(free, jira_on=False), app._epic_created)
        await pilot.pause()
        assert isinstance(app.screen, tui.NewEpicScreen)
        app.screen.query_one("#key").value = "SMOKE-1"
        app.screen.query_one("#summary").value = "smoke epic"
        await pilot.click("#ok")
        await pilot.pause(); await pilot.pause()
        s = cc.load_state()
        assert "SMOKE-1" in s["epics"], "epic NOT created via modal"
        print("TUI smoke OK: %d project(s); free-epic created via modal on '%s'" % (nproj, free))
        s["epics"].pop("SMOKE-1", None); cc.save_state(s)

asyncio.run(main())
