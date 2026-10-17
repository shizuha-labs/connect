/**
 * Mono section kicker used across Connect chrome (CON-276 / design §4.1).
 * Example: CONNECT · FEED
 */
export default function SectionKicker({ children, className = '' }) {
  return (
    <div
      className={`font-mono text-[10px] font-medium uppercase tracking-[0.16em] text-gray-600 dark:text-gray-400 ${className}`}
    >
      {children}
    </div>
  )
}
