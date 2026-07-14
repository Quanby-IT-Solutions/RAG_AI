import { defineConfig } from "eslint/config"

import { appConfig, restrictEnvAccess } from "@repo/eslint-config/next"

/** @type {import("eslint").Linter.Config[]} */
export default defineConfig(
	{
		ignores: [".next/**"],
	},
	appConfig,
	restrictEnvAccess
)
