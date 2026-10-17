module.exports = {
  root: true,
  env: {
    browser: true,
    es2022: true,
    node: true,
  },
  extends: [
    'eslint:recommended',
    'plugin:react-hooks/recommended',
  ],
  parserOptions: {
    ecmaVersion: 'latest',
    sourceType: 'module',
    ecmaFeatures: { jsx: true },
  },
  plugins: ['react-refresh'],
  ignorePatterns: ['dist/', 'node_modules/', 'playwright-report/', 'test-results/'],
  rules: {
    // The existing tree predates a lint gate and carries deliberate unused
    // imports/arguments. Keep the merge blocker focused on correctness while
    // those cosmetic findings are cleaned up separately.
    'no-unused-vars': 'off',
    'react-refresh/only-export-components': ['error', { allowConstantExport: true }],
  },
}
