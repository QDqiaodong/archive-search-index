# Archive Search Index

为离线档案整理人员建立可追溯的段落全文检索库。输入文档清单及UTF-8原文，输出独立
SQLite FTS5索引；检索返回文档身份、集合、完整命中段落、物理行号及原文SHA256。
原始字节保存在数据库中，移走输入后仍可检索和核对来源。

## 安装

Python 3.11+，其SQLite编译版本必须启用FTS5。无第三方运行或测试依赖。
使用独立虚拟环境，不修改全局包：

```sh
python3 --version
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements-build.lock
.venv/bin/python -m pip install --no-build-isolation --no-deps .
.venv/bin/python -m pip check
```

构建工具固定setuptools 80.9.0与wheel 0.45.1；首次准备可能访问公共PyPI，实际测试和演示
不需网络。编辑源码后重新安装。不要使用macOS可能仍指向3.9的python3，需先确认版本。

## 离线演示

在仓库根目录执行：

```sh
.venv/bin/archive-search build --manifest fixtures/manifest.json --database run/archive.sqlite
.venv/bin/archive-search search --database run/archive.sqlite --query '"winter maintenance"' --collection engineering
.venv/bin/archive-search search --database run/archive.sqlite --query restoration --collection library
.venv/bin/python -m unittest discover -s tests -v
```

构建结果为3份文档、5个段落。短语查询返回A1第2段（第4行）与A3第1段（第1–2行）；
library集合中的restoration查询只命中A2第1段。顺序由SQLite BM25排名决定，
同分按文档ID/段号排序；排名值不是概率，不承诺跨SQLite版本数值一致。
重复全量构建与查询得到相同逻辑结果；不承诺SQLite文件字节完全相同。
`python -m archive_index.cli`与安装的命令行入口等价。

## 可重放性质回归与变异验收

在仓库根目录执行一条离线命令：

```sh
PYTHONPATH=$PWD/src python3 tools/acceptance.py
```

该命令先跑完整测试套件，再用逐项隔离的变异运行器注入五种可运行缺陷并判定
新增回归断言能否杀死；源码不被修改，证据写入`run/acceptance-report.json`。
性质语料由固定种子生成（重复正文、仅标题命中、空行与CRLF、多集合），
段落/行号/摘要及AND/OR/NOT参考求值均独立从原始字节推导，不调用被测解析或
查询函数。失败会保存语料、查询、种子与单例重放命令；实际CLI亦被覆盖。
详见[性质与变异验收](docs/property-acceptance.md)。

## 引用迁移

档案改版后旧行号不能直接沿用。`migrate`在只读单快照下核验新旧两库
（原文SHA256及从原文重建的段落/行号），核对严格JSON引文与旧库事实一致，
再按同文档ID对齐：段落按LF全文精确相等建立保序一对一最大匹配，并用动态规划
刻画全部最优匹配（从不枚举对齐、不按最近行号猜选）。每个最优解均映到同一新段
才可迁移；从不匹配或文档消失为失效；其余为歧义并列出全部候选及是否可能未匹配。
确定性报告保留双侧摘要、带行号段落序列、旧引文、全部候选与最大匹配数，
移走输入仍能复核；坏引文/坏库整次失败，写出失败保旧报告，禁止覆盖输入。

```sh
.venv/bin/archive-search build --manifest fixtures/migration/old/manifest.json --database run/old.sqlite
.venv/bin/archive-search build --manifest fixtures/migration/new/manifest.json --database run/new.sqlite
.venv/bin/archive-search migrate --old run/old.sqlite --new run/new.sqlite \
  --citations fixtures/migration/citations.json --report run/migration-report.json
```

示例结果为5条引文：3条迁移（M1插段后行号顺移、M3唯一尾段、M4不变）、
1条歧义（M3重复段丢一份）、1条失效（M2文档消失；新库M5虽有逐字相同文本，
跨文档从不对齐）。详见[迁移契约](docs/migration.md)。

## 馆藏选词：集合用语对照

`contrast`比较同一快照中两个集合的正文词项，用于选词。只统计现有FTS5索引的
`body`列，标题既不贡献词项也不贡献出现次数；词频、含词文档数直接由SQLite在
索引上聚合，大小写、重音与中文边界完全沿用建库分词器，不在Python里重新拆词。
每个词按`((DF_A+1)/(N_A+2))/((DF_B+1)/(N_B+2))`降序排名，同值按词项Unicode
顺序；比值用整数交叉相乘精确比较并以约分后的分子/分母输出，不做浮点排序。
报告含两集文档数与每个词的两侧TF/DF，只读单事务快照，不读原文件，词项实例
不进入Python内存；失败或写出失败保旧报告，绝不覆盖源库。

```sh
.venv/bin/archive-search build --manifest fixtures/contrast/manifest.json --database run/contrast.sqlite
.venv/bin/archive-search contrast --database run/contrast.sqlite \
  --collection-a alpha --collection-b beta --limit 100 --report run/contrast-report.json
```

合成样例含跨段重复、同文异ID、仅标题出现的词、重音合并（Café/CAFÉ）、
比分并列与中文单词；`fixtures/contrast/expected.json`是人工核对的完整报告，
实跑记录见[集合用语对照验证](docs/contrast-validation.md)。
详见[集合用语对照契约](docs/contrast.md)。

## 模块

- `ingest.py`：受限本地输入捕获、严格清单校验与段落行号；清单元数据先完整校验，
  正文按需逐文档单次读取，建库内存随单文档而非总语料增长（总量上限1GiB）。
- `store.py`：流式逐文档捕获/校验/入库，FTS5物化与完整快照原子发布。
- `query.py`：只读短语/布尔检索、排名和集合过滤。
- `migrate.py`：只读快照核验、全最优匹配刻画与确定性迁移报告。
- `contrast.py`：只读单快照的集合正文词项对照、精确有理数排名与确定性报告。
- `fixtures/`：MIT许可的自制虚构档案，不包含真实客户材料。
- `tests/`：数据结果、跨进程CLI、错误输入与替换失败测试。

详见[数据契约](docs/contract.md)。建库为逐文档有界内存流式入库（清单1MiB、单份2MiB、
总量1GiB、至多1000份），峰值随单文档增长，实测见[流式入库验证](docs/streaming-validation.md)。
单操作者全量快照，不对活跃读写数据库发布；
当前无增量同步、PDF/OCR解析、语义向量或搜索服务器。FTS5 unicode61不是中文分词器；
连续汉字作为token，不支持任意中文子串搜索。结果是纯文本，不生成HTML。
未来可按真实需求演进文档获取、索引组织或检索效果分析，当前不声称这些功能已实现。
