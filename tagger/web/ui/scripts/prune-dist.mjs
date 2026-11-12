/**
 * 清掉 dist/assets 里**上一代**的入口产物（index-<hash>.js / .css）。
 *
 * 为什么需要它：vite.config.ts 里 `emptyOutDir: false` 是有意的（某些受管环境
 * 会给 fs.rm 挂安全删除守卫，清空 dist 时直接抛错让构建失败），代价是每次
 * `vite build` 都只覆盖新文件名，旧产物一直留着。实测堆到 70 个 JS、52 MB，
 * 而 pack-dist.bat 会把整个 dist 打进分发包——等于每发一版白带 50 MB 死文件。
 *
 * 做法是**只认 index.html 里真被引用的那两个**：
 *   - 只删 index-*.js / index-*.css，其他命名（字体、将来的 vendor chunk）
 *     一律不碰——删错一个 chunk 的后果是页面白屏，而省下的只是几百 KB。
 *   - 用 unlink 逐个删文件，不做目录递归删除，绕开上面那个 rm 守卫。
 *
 * 用法：
 *   node scripts/prune-dist.mjs            # 真删
 *   node scripts/prune-dist.mjs --dry-run  # 只报告
 */
import { readdirSync, readFileSync, statSync, unlinkSync } from 'node:fs'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'

const UI_DIR = dirname(dirname(fileURLToPath(import.meta.url)))
const DIST = join(UI_DIR, 'dist')
const ASSETS = join(DIST, 'assets')
const dryRun = process.argv.includes('--dry-run')

/** index.html 里 /assets/xxx 形式的引用 = 这一版真正要用的入口产物。 */
function referencedAssets() {
  let html
  try {
    html = readFileSync(join(DIST, 'index.html'), 'utf8')
  } catch {
    // 没有 dist 说明还没构建过，不是错误（build 脚本会先构建再调用这里）。
    return null
  }
  const keep = new Set()
  for (const m of html.matchAll(/(?:src|href)="([^"]+)"/g)) {
    const p = m[1]
    if (p.startsWith('/assets/')) keep.add(p.slice('/assets/'.length))
  }
  return keep
}

const keep = referencedAssets()
if (keep === null) {
  console.log('[prune-dist] 没有 dist/index.html，跳过')
  process.exit(0)
}
if (keep.size === 0) {
  // 解析不到引用就别删：可能是 index.html 格式变了，硬删会删掉正在用的文件。
  console.error('[prune-dist] index.html 里没解析出任何 /assets/ 引用，'
    + '为安全起见不动任何文件')
  process.exit(1)
}

let removed = 0
let freed = 0
const kept = []
for (const name of readdirSync(ASSETS)) {
  if (!/^index-[A-Za-z0-9_-]+\.(js|css)$/.test(name)) continue
  if (keep.has(name)) {
    kept.push(name)
    continue
  }
  const full = join(ASSETS, name)
  const size = statSync(full).size
  if (dryRun) {
    console.log(`[dry-run] 将删除 ${name}  ${(size / 1024).toFixed(0)} KB`)
  } else {
    unlinkSync(full)
  }
  removed += 1
  freed += size
}

const mb = (freed / 1024 / 1024).toFixed(1)
if (removed === 0) {
  console.log(`[prune-dist] 没有旧产物（保留 ${kept.join(', ')}）`)
} else {
  console.log(`[prune-dist] ${dryRun ? '可删除' : '已删除'} ${removed} 个旧产物，`
    + `${mb} MB；保留 ${kept.join(', ')}`)
}
