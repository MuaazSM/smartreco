import type { Config } from "tailwindcss";

const config: Config = {
  content: [
    "./app/**/*.{ts,tsx}",
    "./components/**/*.{ts,tsx}",
    "./lib/**/*.{ts,tsx}",
  ],
  theme: {
    extend: {
      fontFamily: {
        sans: ["var(--font-sans)", "ui-sans-serif", "system-ui", "sans-serif"],
        display: ["var(--font-display)", "ui-serif", "Georgia", "serif"],
        mono: ["var(--font-mono)", "ui-monospace", "SFMono-Regular", "monospace"],
      },
      // Larger, intentional type scale. Overrides the small default keys (all body/UI copy
      // sat at text-sm=14 / text-xs=12) while keeping Tailwind's larger keys (6xl+) intact.
      fontSize: {
        xs: ["0.875rem", { lineHeight: "1.5" }], // 14
        sm: ["1rem", { lineHeight: "1.6" }], // 16 — most of the app's body/UI text
        base: ["1.0625rem", { lineHeight: "1.65" }], // 17
        lg: ["1.1875rem", { lineHeight: "1.55" }], // 19
        xl: ["1.375rem", { lineHeight: "1.4" }], // 22
        "2xl": ["1.75rem", { lineHeight: "1.25", letterSpacing: "-0.01em" }], // 28
        "3xl": ["2.25rem", { lineHeight: "1.15", letterSpacing: "-0.015em" }], // 36
        "4xl": ["2.75rem", { lineHeight: "1.1", letterSpacing: "-0.02em" }], // 44
        "5xl": ["3.5rem", { lineHeight: "1.03", letterSpacing: "-0.02em" }], // 56
      },
      colors: {
        // Design-system tokens, all swapped light/dark via CSS vars (see globals.css) so
        // components can say `bg-surface border-hairline text-muted text-accent` and stay
        // in sync with the theme + the manual toggle. Raw Tailwind palette classes are
        // avoided in restyled components in favor of these.
        paper: "var(--paper)",
        surface: "var(--surface)",
        ink: "var(--ink)",
        muted: "var(--muted)",
        faint: "var(--faint)",
        hairline: {
          DEFAULT: "var(--hairline)",
          strong: "var(--hairline-2)",
        },
        accent: {
          DEFAULT: "var(--accent)",
          soft: "var(--accent-soft)",
        },
        ok: "var(--ok)",
        neg: "var(--neg)",
        // The always-dark console surface.
        con: {
          bg: "var(--con-bg)",
          panel: "var(--con-panel)",
          edge: "var(--con-edge)",
          ink: "var(--con-ink)",
          mut: "var(--con-mut)",
          key: "var(--con-key)",
          ok: "var(--con-ok)",
          str: "var(--con-str)",
        },
      },
      maxWidth: {
        page: "var(--maxw)",
      },
      boxShadow: {
        // The soft lift the hero recommendation card sits on in the artifact.
        artifact: "0 24px 60px -34px rgba(20, 25, 60, 0.28)",
      },
    },
  },
  plugins: [],
};

export default config;
