# 待删清单（统一手动删）

这里的文件**我删不掉**，需要你手动清理。

## 为什么删不掉

这个沙箱把**所有删除**都交给一个"安全删除"代理，它要求走 Windows 回收站；沙箱里回收站不可用，
于是代理按设计拒绝（`SAFE_DELETE_FAIL_CLOSED ... windows-sandbox-recycle-bin-unavailable`）。

**对照实验（可复现）**：在同一目录新建一个文件后立刻删除，同样失败：

```
created: True
unlink FAILED: OSError [safe-delete][SAFE_DELETE_FAIL_CLOSED] {"target": "backend\\tests\\delete-probe.txt",
                "reason": "windows-sandbox-recycle-bin-unavailable"}
```

所以不是文件被占用、也不是只读属性（`attrib` 显示两个文件都只有 `A`）。
**这个环境里"创建可以、删除不行"** —— 因此"移动进本文件夹"也做不到（移动 = 新建 + 删旧）。
下面按**原路径**列出，删的时候直接按路径删即可。

## 待删

| 路径 | 为什么在这里 | 备注 |
|---|---|---|
| **`.git/index.lock`** | **阻塞 git**：我的 `git add -A` 重试时被沙箱打断，留下了写了一半的索引锁（7929 字节，21:39） | **必须先删，否则任何 git 写操作都会报 "File exists"**。确认没有 git 进程在跑即可安全删除 —— 这是标准恢复步骤 |
| `backend/tests/test_mcp.staged` | 一次原子写留下的暂存文件；写入目标文件被瞬时拒绝后，暂存副本留了下来 | 已在 `.gitignore` 里显式忽略，**不会被提交** |
| `backend/tests/delete-probe.txt` | 我为上面那个"对照实验"新建的探针文件，之后删不掉 | 已在 `.gitignore` 里显式忽略 |

清理命令（在你那边执行即可）：

```bash
cd C:/Users/ooo/Desktop/re0
rm -f .git/index.lock                          # 先删这个，git 才能用
rm -f backend/tests/test_mcp.staged backend/tests/delete-probe.txt
git status --short                              # 确认锁已清、工作区只剩余下待提交的改动
```

## 以后怎么用

我遇到删不掉的东西，会往这里加一行（路径 + 原因 + 是否已被 gitignore），不再囤在别处。
你定期照表清理一次就行。清理完可以把表里的行删掉，保留这个文件本身作为约定。
