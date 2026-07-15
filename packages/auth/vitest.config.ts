import { configDefaults, defineConfig } from "vitest/config"
import path from "path"

export default defineConfig({
	test: {
		environment: "node",
		globals: true,
		passWithNoTests: true,
		// Never pick up stale copies of specs from build output.
		exclude: [...configDefaults.exclude, "**/dist/**"],
		// First spec per file pays better-auth's module evaluation cost, which
		// can exceed the 5s default on slower machines.
		testTimeout: 15000,
		coverage: {
			provider: "v8",
			reporter: ["text", "json", "json-summary", "html"],
			thresholds: {
				lines: 80,
				functions: 80,
				branches: 30,
				statements: 80,
			},
			exclude: [
				"**/*.config.*",
				"**/*.setup.*",
				"**/*.spec.*",
				"**/*.test.*",
				"**/*.d.ts",
				"**/node_modules/**",
			],
		},
	},
	resolve: {
		alias: {
			"@": path.resolve(__dirname, "src"),
		},
	},
})
