import { UnsupportedMediaTypeException } from "@nestjs/common"
import type { MulterOptions } from "@nestjs/platform-express/multer/interfaces/multer-options.interface"
import { memoryStorage } from "multer"

/**
 * Gateway-enforced upload limits (Tech Plan §5). Nginx already caps the raw
 * body at 26m (multipart headroom); Multer enforces the exact 25 MB file
 * limit and the supported-type allow-list BEFORE anything is forwarded to the
 * RAG service, which re-checks both defensively (app/loaders.py is the Python
 * source of truth — keep the two lists in sync).
 */
export const MAX_UPLOAD_BYTES = 25 * 1024 * 1024

/** Supported source formats (IN-1 PDF + IN-3 HTML/DOCX/CSV). */
export const SUPPORTED_UPLOAD_MIME_TYPES = [
	"application/pdf",
	"text/html",
	"application/vnd.openxmlformats-officedocument.wordprocessingml.document",
	"text/csv",
	"application/csv",
] as const

const SUPPORTED_MIME_SET: ReadonlySet<string> = new Set(SUPPORTED_UPLOAD_MIME_TYPES)

/** Reject unsupported parts with a clear 415 before the request body is buffered. */
export const uploadFileFilter: NonNullable<MulterOptions["fileFilter"]> = (
	_req,
	file,
	callback
) => {
	const mime = (file.mimetype ?? "").split(";")[0]?.trim().toLowerCase() ?? ""
	if (!SUPPORTED_MIME_SET.has(mime)) {
		callback(
			new UnsupportedMediaTypeException(
				`Unsupported file type "${file.mimetype}". Send a single file in the "file" field with one of: ${SUPPORTED_UPLOAD_MIME_TYPES.join(", ")}.`
			),
			false
		)
		return
	}
	callback(null, true)
}

/**
 * Multer memory storage: the file is held in memory only long enough to be
 * forwarded to the Python RAG service (no disk writes on the gateway).
 * `files: 1` rejects multi-file submissions; oversize triggers Multer's
 * LIMIT_FILE_SIZE, which Nest translates to 413 Payload Too Large.
 */
export const documentUploadOptions: MulterOptions = {
	storage: memoryStorage(),
	limits: { fileSize: MAX_UPLOAD_BYTES, files: 1 },
	fileFilter: uploadFileFilter,
}
