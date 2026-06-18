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

        # select the first project node (multi-project safe), then open New Epic
        await pilot.press("down")
        await pilot.pause()
        await pilot.press("e")
        await pilot.pause()
        assert isinstance(app.screen, tui.NewEpicScreen), "epic modal did not open"

        # fill + create via the Create button
        app.screen.query_one("#key").value = "SMOKE-1"
        app.screen.query_one("#summary").value = "smoke epic"
        await pilot.click("#ok")
        await pilot.pause()
        await pilot.pause()

        s = cc.load_state()
        assert "SMOKE-1" in s["epics"], "epic NOT created via TUI"
        print("TUI smoke OK: %d project(s); epic created via modal (SMOKE-1)" % nproj)

        # cleanup the throwaway epic
        s["epics"].pop("SMOKE-1", None)
        cc.save_state(s)

asyncio.run(main())
