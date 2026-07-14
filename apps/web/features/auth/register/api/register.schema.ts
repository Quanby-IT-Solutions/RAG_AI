import { z } from "zod"

export const RegisterSchema = z.object({
	name: z.string().min(1, "Name is required"),
	email: z.email("Please enter a valid email address"),
	password: z.string().min(8, "Password must be at least 8 characters"),
})

export type Register = z.infer<typeof RegisterSchema>
