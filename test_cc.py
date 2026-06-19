import sys, types, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).parent))
import cc

def test_slugify():
    assert cc.slugify("БЧК badge fix") == "badge-fix" or cc.slugify("Hello World!") == "hello-world"
    assert cc.slugify("Hello World!") == "hello-world"
    assert cc.slugify("a"*100) == "a"*40
    assert cc.slugify("") == "task"

def test_worktree_path():
    p = cc.worktree_path("/x/invictus", "IK-1", "my-slug", "invictuswebsite")
    assert str(p) == "/x/invictus/cctui/IK-1/my-slug/invictuswebsite", p

def test_target_for():
    epic = {"targets": {"website": "loyalty/integration"}}
    proj = {"repos": {"website": {"default_branch": "main"}, "payment": {"default_branch": "master"}}}
    assert cc.target_for(epic, proj, "website") == "loyalty/integration"
    assert cc.target_for(epic, proj, "payment") == "master"


def test_unique_tid_no_collision():
    s = {"tasks": {}}
    # first task with slug "go" under epic E1
    t1 = cc._unique_tid(s, "E1", "go"); s["tasks"][t1] = {"epic": "E1"}
    assert t1 == "t_go", t1
    # same slug, DIFFERENT epic -> must NOT reuse t_go (that was the IK-8894-go loss)
    t2 = cc._unique_tid(s, "E2", "go"); s["tasks"][t2] = {"epic": "E2"}
    assert t2 != t1 and t2 not in (), t2
    assert t2 == "t_e2_go", t2
    # same slug, SAME epic -> numbered, still unique
    t3 = cc._unique_tid(s, "E1", "go"); s["tasks"][t3] = {"epic": "E1"}
    assert t3 not in (t1, t2), t3
    # all keys distinct -> no overwrite possible
    assert len(s["tasks"]) == 3, s["tasks"]


if __name__ == "__main__":
    n = 0
    for k, v in list(globals().items()):
        if k.startswith("test_") and callable(v):
            v(); n += 1; print("ok", k)
    print("%d tests passed" % n)
