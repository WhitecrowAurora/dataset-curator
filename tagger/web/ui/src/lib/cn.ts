/** 与 lulynx launcher 同款：只为条件拼接，不做 tailwind-merge（没有冲突场景）。 */
export function cn(...parts: Array<string | false | null | undefined>): string {
  return parts.filter(Boolean).join(' ')
}
