import asyncio, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent))
import tui, cc

async def main():
    app = tui.CCApp()
    async with app.run_test(size=(100, 40)) as pilot:  # realistic terminal; 80x24 clips bottom-docked buttons
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
        # _epic_created runs a blocking subprocess in the dismiss callback;
        # poll a few frames so the smoke is deterministic, not timing-flaky.
        # _epic_created spawns a cold `python cc.py` subprocess in the dismiss callback;
        # first cold spawn can take ~1-2s, so wait generously (this is harness timing, not app behavior).
        for _ in range(100):
            await pilot.pause()
            await asyncio.sleep(0.1)
            if "SMOKE-1" in cc.load_state()["epics"]:
                break
        s = cc.load_state()
        assert "SMOKE-1" in s["epics"], "epic NOT created via modal"
        print("TUI smoke OK: %d project(s); free-epic created via modal on '%s'" % (nproj, free))
        s["epics"].pop("SMOKE-1", None); cc.save_state(s)

asyncio.run(main())
