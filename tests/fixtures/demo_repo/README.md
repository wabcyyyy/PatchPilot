# demo_repo

PatchPilot 测试夹具仓库:一个带 `parse_date` 函数、3 个测试(其中 1 个基线故意失败)的微型项目。

注意:本目录只保存工作树文件;git 历史由 `tests/conftest.py::make_repo` 在临时目录中
现场构建(`git init` + 两个 commit),避免在主仓库里嵌套 `.git`。
