import type { Transition, Variants } from 'framer-motion'

// 只保留本项目用得到的几个预设，其余（页面切换方向、stagger 列表）没搬。
export const controlTransition: Transition = { type: 'tween', duration: 0.18, ease: [0.16, 1, 0.3, 1] }

export const cardHover = { y: -1 }
export const cardTap = { y: 1 }

export const fadeIn: Variants = {
  hidden: { opacity: 0 },
  visible: { opacity: 1, transition: { duration: 0.18 } },
  exit: { opacity: 0, transition: { duration: 0.14 } },
}
