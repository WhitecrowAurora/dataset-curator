// 令牌命名与 lulynx-trainer 的 launcher 一致（--lx-* / bg-lx-* / text-lx-*），
// 这样两边的组件片段可以直接互相搬运，不用改类名。
// 色值仍是 tagger 自己的深色工业底 + PyTorch 橙（见 src/styles/tokens.css）。
const lx = (name) => `color-mix(in srgb, var(${name}) calc(<alpha-value> * 100%), transparent)`

/** @type {import('tailwindcss').Config} */
export default {
  content: ['./index.html', './src/**/*.{ts,tsx}'],
  // 本项目主题全靠 CSS 变量（:root 深色 / [data-theme="light"] 浅色），页面里不写 dark: 变体。
  darkMode: 'class',
  theme: {
    extend: {
      colors: {
        lx: {
          bg: lx('--lx-bg'),
          surface: lx('--lx-surface'),
          'surface-raised': lx('--lx-surface-raised'),
          input: lx('--lx-input'),
          accent: lx('--lx-accent'),
          'accent-light': lx('--lx-accent-light'),
          'accent-dark': lx('--lx-accent-dark'),
          'accent-subtle': lx('--lx-accent-subtle'),
          secondary: lx('--lx-secondary'),
          success: lx('--lx-success'),
          'success-subtle': lx('--lx-success-subtle'),
          warning: lx('--lx-warning'),
          'warning-subtle': lx('--lx-warning-subtle'),
          'warning-bg': lx('--lx-warning-bg'),
          danger: lx('--lx-danger'),
          'danger-subtle': lx('--lx-danger-subtle'),
          'console-bg': lx('--lx-console-bg'),
        },
      },
      textColor: {
        lx: {
          DEFAULT: lx('--lx-text'),
          secondary: lx('--lx-text-secondary'),
          muted: lx('--lx-text-muted'),
          dim: lx('--lx-text-dim'),
          accent: lx('--lx-accent-text'),
          'on-accent': lx('--lx-on-accent'),
          success: lx('--lx-success-text'),
          warning: lx('--lx-warning-text'),
          danger: lx('--lx-danger-text'),
        },
      },
      borderColor: {
        lx: {
          DEFAULT: lx('--lx-border'),
          card: lx('--lx-border-card'),
          subtle: lx('--lx-border'),
          hover: lx('--lx-border-hover'),
          accent: lx('--lx-accent-border'),
          success: lx('--lx-success-border'),
          warning: lx('--lx-warning-border'),
          danger: lx('--lx-danger-border'),
        },
      },
      boxShadow: {
        'lx-glow': '0 0 20px var(--lx-accent-shadow), 0 4px 12px var(--lx-accent-shadow)',
        'lx-card-hover': '0 8px 24px rgba(0,0,0,0.15), 0 0 0 1px var(--lx-accent-ring)',
        'lx-btn': '0 2px 8px var(--lx-accent-glow)',
      },
      borderRadius: {
        lx: 'var(--lx-radius, 8px)',
        'lx-sm': 'var(--lx-radius-sm, 6px)',
        'lx-lg': 'var(--lx-radius-lg, 12px)',
      },
      fontFamily: {
        sans: ['var(--lx-font-sans)'],
        display: ['var(--lx-font-display)'],
        mono: ['var(--lx-font-mono)'],
      },
    },
  },
  plugins: [],
}
