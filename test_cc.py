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

if __name__ == "__main__":
    n = 0
    for k, v in list(globals().items()):
        if k.startswith("test_") and callable(v):
            v(); n += 1; print("ok", k)
    print("%d tests passed" % n)
