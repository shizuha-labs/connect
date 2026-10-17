/** @type {import('tailwindcss').Config} */
export default {
  darkMode: 'class',
  content: [
    "./index.html",
    "./src/**/*.{js,ts,jsx,tsx}",
    // Include shared packages for class detection — @shizuha/ui installed from
    // the npm registry (canonical @shizuha/ui/vite boundary, ORIG-231/ORIG-301).
    "./node_modules/@shizuha/ui/dist/**/*.js",
  ],
  theme: {
    extend: {
      colors: {
        primary: {
          50: '#eef2ff',
          100: '#e0e7ff',
          200: '#c7d2fe',
          300: '#a5b4fc',
          400: '#818cf8',
          500: '#6366f1',
          600: '#4f46e5',
          700: '#4338ca',
          800: '#3730a3',
          900: '#312e81',
          // CON-408 (P2): the scale previously stopped at 900, so every
          // `dark:bg-primary-950/…` utility (thread bubbles, composer, list
          // rows, onboarding cards) generated NO CSS — those surfaces kept
          // their light bg-primary-50 under `.dark`. Indigo-950 per the
          // Tailwind palette the scale mirrors.
          950: '#1e1b4b',
        },
      },
    },
  },
  plugins: [],
}
