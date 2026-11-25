/** 发色示意仅供视觉比较，浅色组同时展示白、银、灰、金。 */
export function HairColorSample({ color, swatch }: { color: string; swatch: string }) {
  return (
    <svg viewBox="0 0 80 80" className="h-14 w-14 shrink-0" role="img" aria-label="发色示意">
      <ellipse cx="40" cy="44" rx="21" ry="27" fill="#e6cbb9" />
      <path d="M15 68V32C15 7 65 7 65 32V68L55 58V31L46 41L43 28L30 43L25 29V60Z" fill={swatch} stroke="#777" strokeWidth=".6" />
      <path d="M22 30Q26 17 38 16M57 25Q53 17 48 17" fill="none" stroke="white" strokeOpacity=".35" strokeWidth="3" />
      <path d="M30 49h5m10 0h5" stroke="#5b4943" strokeWidth="1.8" strokeLinecap="round" />
      {color === 'light' && ['#eeeef2', '#c8cdd6', '#92949c', '#e7cb82'].map((fill, i) => (
        <rect key={fill} x={12 + i * 14} y="71" width="13" height="6" rx="1" fill={fill} />
      ))}
    </svg>
  )
}
