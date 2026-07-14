/** @typedef {import("prettier").Config} PrettierConfig */
/** @typedef {import("prettier-plugin-tailwindcss").PluginOptions} TailwindConfig */
/** @typedef {import("@ianvs/prettier-plugin-sort-imports").PluginConfig} SortImportsConfig */

/**
 * Universal Prettier configuration for all project types
 * Works for: Next.js apps, Node.js APIs, and shared packages
 *
 * @type {PrettierConfig | SortImportsConfig | TailwindConfig}
 */
const config = {
	// Modern formatting preferences
	bracketSameLine: false,
	bracketSpacing: true,
	arrowParens: "avoid",
	endOfLine: "lf",
	printWidth: 100,
	quoteProps: "consistent",
	semi: false,
	singleQuote: false,
	tabWidth: 2,
	trailingComma: "es5",
	useTabs: true,
	proseWrap: "preserve",
	htmlWhitespaceSensitivity: "css",

	// Plugins (Tailwind must be last)
	plugins: ["@ianvs/prettier-plugin-sort-imports", "prettier-plugin-tailwindcss"],

	// Tailwind configuration
	tailwindFunctions: ["cn", "cva"],

	// Universal import order that works for all project types
	importOrder: [
		// 1. Framework imports (React, Next.js, Expo, NestJS)
		"^(react/(.*)$)|^(react$)|^(react-native(.*)$)",
		"^(next/(.*)$)|^(next$)",
		"^(expo(.*)$)|^(expo$)",
		"^@nestjs/(.*)$",
		// 2. Node.js built-in modules
		"<BUILTIN_MODULES>",
		// 3. Third-party packages
		"<THIRD_PARTY_MODULES>",
		"",
		// 4. Monorepo packages
		"^@repo/(.*)$",
		"",
		// 5. Internal path aliases
		"^@/core/(.*)$",
		"^@/services/(.*)$",
		"^@/features/(.*)$",
		"^@/(.*)$",
		"",
		// 6. Parent and sibling imports
		"^~/",
		"^[../]",
		"^[./]",
		"",
		// 7. CSS/Style imports last
		"^(?!.*[.]css$)[./].*$",
		".css$",
	],

	importOrderParserPlugins: ["typescript", "jsx", "decorators-legacy"],
	importOrderTypeScriptVersion: "5.0.0",

	// File-specific overrides
	overrides: [
		{
			files: "*.json.hbs",
			options: {
				parser: "json",
			},
		},
		{
			files: "*.ts.hbs",
			options: {
				parser: "babel",
			},
		},
	],
}

export default config
