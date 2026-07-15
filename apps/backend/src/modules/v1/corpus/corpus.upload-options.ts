import { UnsupportedMediaTypeException } from "@nestjs/common"
import type { MulterOptions } from "@nestjs/platform-express/multer/interfaces/multer-options.interface"
import { memoryStorage } from "multer"

/**
 * Gateway-enforced upload limits (Tech Plan §5). Nginx already caps the raw
 * body at 26m (multipart headroom); Multer enforces the exact 25 MB file
 * limit and PDF-only type BEFORE anything is forwarded to the RAG service,
 * which re-checks both defensively.
 */
export const MAX_PDF_UPLOAD_BYTES = 25 * 1024 * 1024

export const PDF_MIME_TYPE = "application/pdf"

/** Reject non-PDF parts with a clear 415 before the request body is buffered. */
export const pdfFileFilter: NonNullable<MulterOptions["fileFilter"]> = (_req, file, callback) => {
	if (file.mimetype !== PDF_MIME_TYPE) {
		callback(
			new UnsupportedMediaTypeException(
				`Only PDF uploads are accepted (got "${file.mimetype}"). Send a single application/pdf file in the "file" field.`
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
export const pdfUploadOptions: MulterOptions = {
	storage: memoryStorage(),
	limits: { fileSize: MAX_PDF_UPLOAD_BYTES, files: 1 },
	fileFilter: pdfFileFilter,
}
