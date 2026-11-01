import { defineComponent, h, type VNodeChild } from 'vue'
import MarkdownIt from 'markdown-it'

// Render a small, explicitly allowlisted token tree. No generated HTML enters innerHTML.
const markdown = new MarkdownIt('commonmark', { html: false, breaks: true, linkify: false }).enable('table').disable('image')
const tags = new Set(['p', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6', 'strong', 'em', 's', 'ul', 'ol', 'li', 'blockquote', 'table', 'thead', 'tbody', 'tr', 'th', 'td', 'a'])
export function replyPreview(text: string, kind?: string) {
  let long = text.length > 900 || text.split('\n').length > 18
  let preview = text, overview = false
  const tokens = markdown.parse(text, {})
  if (['itinerary-draft', 'revision'].includes(kind || '')) {
    const boundary = tokens.find(t => t.type === 'hr' && t.level === 0 && t.map)
    if (boundary?.map) { const lines = text.split('\n'); const prefix = lines.slice(0, boundary.map[0]).join('\n'); if (prefix.trim() && prefix.length <= 650 && lines.slice(boundary.map[1]).join('\n').trim()) { preview = prefix; long = true; overview = true } }
  }
  if (long && !overview) {
    const index = tokens.findIndex(t => t.type === 'paragraph_open')
    const block = index >= 0 ? tokens[index + 1] : tokens.find(t => t.map && ['fence', 'code_block', 'inline'].includes(t.type))
    if (block?.map) preview = text.split('\n').slice(0, block.map[1]).join('\n')
    long = preview !== text
  }
  return { long, preview, overview }
}
export function safeHref(value: string) { try { const url = new URL(value); return ['http:', 'https:'].includes(url.protocol) ? url.href : undefined } catch { return undefined } }
export default defineComponent({
  name: 'SafeMarkdown', props: { text: { type: String, default: '' } },
  setup(props) {
    return () => {
      type Token = ReturnType<typeof markdown.parse>[number]
      function render(tokens: Token[]): VNodeChild[] {
        const root: VNodeChild[] = [], stack = [root]
        for (const token of tokens) {
          const children = stack[stack.length - 1]!
          if (token.nesting === -1) { if (stack.length > 1) stack.pop(); continue }
          if (token.type === 'inline') { children.push(...render(token.children ?? [])); continue }
          if (token.type === 'text' || token.type === 'html_inline' || token.type === 'html_block') { children.push(token.content); continue }
          if (token.type === 'softbreak' || token.type === 'hardbreak') { children.push(h('br')); continue }
          if (token.type === 'code_inline') { children.push(h('code', token.content)); continue }
          if (['fence', 'code_block'].includes(token.type)) { children.push(h('pre', [h('code', token.content)])); continue }
          if (token.type === 'hr') { children.push(h('hr')); continue }
          if (token.nesting === 1) {
            const tag = tags.has(token.tag) ? token.tag : 'span'
            const attrs: Record<string, string | number | undefined> = {}
            if (tag === 'a') { attrs.href = safeHref(token.attrGet('href') || ''); attrs.target = '_blank'; attrs.rel = 'noopener noreferrer'; attrs.referrerpolicy = 'no-referrer' }
            if (tag === 'ol' && token.attrGet('start')) attrs.start = Number(token.attrGet('start'))
            const next: VNodeChild[] = []; children.push(h(tag, attrs, next)); stack.push(next)
          }
        }
        return root
      }
      return h('div', { class: 'safe-markdown' }, render(markdown.parse(props.text, {})))
    }
  },
})
