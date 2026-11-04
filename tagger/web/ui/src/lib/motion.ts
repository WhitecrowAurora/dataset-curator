import type { Transition, Variants } from 'framer-motion'

// 只保留本项目用得到的几个预设，其余（页面切换方向、stagger 列表）没搬。
export const springSnappy: Transition = { type: 'spring', stiffness: 400, damping: 30 }

export const cardHover = { scale: 1.02, y: -2 }
export const cardTap = { scale: 0.98 }

export const fadeIn: Variants = {
  hidden: { opacity: 0 },
  visible: { opacity: 1, transition: { duration: 0.25 } },
  exit: { opacity: 0, transition: { duration: 0.2 } },
}
