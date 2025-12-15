# 代码预览器插件

将代码渲染为精美图片并发送给用户。

## 功能特性

- 主流编程语言的语法高亮
- 多种代码主题可选
- 支持自定义字体、字体大小等
- 自动检测代码语言

使用`Highlight.js`.


<details>
<summary>点击展开</summary>

```plaintext
1.3.2 删除了被Astrbot AI代码检查器诟病的自定义语言解析器
1.4.0 重写了自定义语言(存在问题)
1.4.0-hotfix1 修复了无法检测代码文件的语言问题
1.4.5 修复了代码无法填充整张图片的问题
1.4.7 实现了行号显示
```
</details>

- **支持参数指定渲染选项**

## 使用方法

### 渲染代码

```
/render
[参数]
```

引用一条包含代码的消息，然后发送 `/render`

```
/render_file
[参数]
```

引用一个代码文件，然后发送 `/render_file`
参数同render


### 支持的参数

| 使用 | 说明 | 示例 |
|------|------|------|
| `-l <语言>` | 指定代码语言(自动识别) | `-l python` |
| `-t <主题>` | 指定渲染主题 | `-t dracula` |
| `-s <字号>` | 指定字体大小 | `-s 16` |
| `-ln` | 显式配置启用行号 | `-ln` |
| `-n` | 显式配置禁用行号 | `-n` |
### 示例

使用 Markdown 代码块：

```
/render -t github-dark
​```python
def hello():
    print("Hello, World!")
​```

# 即使不填python,也会自动识别。识别的精准度取决于代码内的语言特性占比。
```

## 配置项

| 配置项 | 说明 | 默认值 |
|--------|------|--------|
| theme | 代码主题 | monokai |
| font_family | 字体名称 | Jetbrains Mono |
| font_size | 字体大小 | 14 |
| padding | 内边距 | 20 |
| blacklist | 群聊黑名单 | [] |

### 支持的主题

浏览仓库目录/assets/highlight/styles即可。

# 自定义语言配置说明

## 概述

此插件支持通过 JSON 文件定义自定义语言，并自动注册到 highlight.js 中进行语法高亮。

## 配置文件位置

将自定义语言的 JSON 文件放置在插件根目录的 `languages/` 文件夹中。文件名（不含扩展名）将作为语言标识符。

例如：`languages/mylang.json` 将注册为 `mylang` 语言。

## JSON 结构

### 必需字段

- **name** (string): 语言的显示名称
- **aliases** (array): 语言的别名列表
- **extensions** (array): 该语言的文件扩展名列表

### 可选字段

#### keywords (object | array)

定义语言的关键字。可以是简单的数组，或按类别分组的对象。

**简单数组形式：**
\`\`\`json
"keywords": ["if", "else", "for", "while", "return"]
\`\`\`

**分类对象形式：**
\`\`\`json
"keywords": {
  "keyword": ["if", "else", "for", "while"],
  "type": ["int", "float", "string", "bool"],
  "literal": ["true", "false", "null"],
  "built_in": ["print", "console", "Math"]
}
\`\`\`

#### strings (object)

配置字符串匹配规则：

\`\`\`json
"strings": {
  "double_quote": true,    // 支持双引号字符串 "..."
  "single_quote": true,    // 支持单引号字符串 '...'
  "backtick": false        // 支持反引号字符串 `...`
}
\`\`\`

默认值：双引号和单引号均为 true，反引号为 false。

#### numbers (object)

配置数字匹配规则：

\`\`\`json
"numbers": {
  "use_default": true,     // 使用 highlight.js 默认数字模式
  "binary": true,          // 支持二进制 0b1010
  "octal": true,           // 支持八进制 0o755
  "hex": true,             // 支持十六进制 0xFF
  "decimal": true          // 支持十进制和浮点数
}
\`\`\`

如果 `use_default` 为 true，将使用 highlight.js 的 C_NUMBER_MODE。

#### patterns (array)

定义额外的语法模式：

\`\`\`json
"patterns": [
  {
    "className": "meta",
    "begin": "@[A-Za-z_][A-Za-z0-9_]*"
  },
  {
    "className": "function",
    "begin": "\\b(fn)\\s+([A-Za-z_][A-Za-z0-9_]*)",
    "keywords": "fn"
  }
]
\`\`\`

每个模式支持的字段：
- **className**: highlight.js 的类名（如 "function", "class", "meta", "comment" 等）
- **begin**: 匹配开始的正则表达式
- **end**: 匹配结束的正则表达式（可选）
- **keywords**: 该模式内的关键字（可选）

## 完整示例

参见 `languages/ljos.json` 和 `languages/esharp.json` 文件。

### 最小示例

\`\`\`json
{
  "name": "MyLang",
  "aliases": ["ml", "mylang"],
  "extensions": [".ml", ".mylang"],
  "keywords": ["if", "else", "for", "while", "fn", "return"],
  "strings": {
    "double_quote": true,
    "single_quote": false
  }
}
\`\`\`

## 使用方法

1. 创建语言定义 JSON 文件
2. 放置到 `languages/` 文件夹
3. 重启插件
4. 使用文件扩展名或语言名称触发高亮

## 注意事项

- 文件名会成为语言标识符，建议使用小写字母和下划线
- aliases 数组中的别名也可以用于识别该语言
- 插件会自动将语言定义注册到 highlight.js
- 语言检测主要依赖 highlight.js 的自动检测功能
- 提供的扩展名会用于文件名匹配

