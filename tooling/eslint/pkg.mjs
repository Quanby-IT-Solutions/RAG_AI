import * as path from "node:path"
import { includeIgnoreFile } from "@eslint/compat"
import prettier from "eslint-config-prettier"
import { defineConfig } from "eslint/config"
import tseslint from "typescript-eslint"

/**
 * ESLint config preset for shared packages and libraries
 * Includes: base TypeScript rules and Prettier integration
 */
export const packageConfig = defineConfig(
	// Ignore files not tracked by VCS and any config files
	includeIgnoreFile(path.join(import.meta.dirname, "../../.gitignore")),
	{ ignores: ["**/*.config.*"] },
	{
		files: ["**/*.js", "**/*.ts", "**/*.tsx"],
		languageOptions: {
			parser: tseslint.parser,
			parserOptions: {
				projectService: true,
			},
		},
		plugins: {
			"@typescript-eslint": tseslint.plugin,
		},
		rules: {
			// Base JavaScript rules
			"no-console": "warn",
			"eqeqeq": ["error", "always"],
			"curly": ["error", "all"],
			"prefer-const": "error",
			"no-var": "error",
			"object-shorthand": "error",
			"prefer-template": "error",
			"no-unreachable": "warn",
			"no-duplicate-imports": "error",
			"no-unused-expressions": "error",
			"no-return-await": "error",
			"no-eval": "error",
			"no-implied-eval": "error",
			"no-new-func": "error",
			"no-script-url": "error",

			// TypeScript-specific rules
			"@typescript-eslint/array-type": "off",
			"@typescript-eslint/consistent-type-definitions": "off",
			"@typescript-eslint/consistent-type-imports": [
				"warn",
				{ prefer: "type-imports", fixStyle: "inline-type-imports" },
			],
			"@typescript-eslint/no-unused-vars": ["warn", { argsIgnorePattern: "^_" }],
			"@typescript-eslint/no-explicit-any": "warn",
			"@typescript-eslint/require-await": "off",
			"@typescript-eslint/no-misused-promises": [
				"error",
				{ checksVoidReturn: { attributes: false } },
			],
		},
	},
	// Prettier (must be last to override conflicting rules)
	prettier
)
