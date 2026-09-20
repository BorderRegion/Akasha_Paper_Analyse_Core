/**
 * Turning any thrown value into a displayable error code/message.
 *
 * A page must never assume that a rejected promise is an `ApiError`: a transport
 * failure, an aborted request or a bug in a hook is still an error the user has
 * to see, and reaching into `error.shape.code` blindly crashes the whole route
 * (which then shows an empty error boundary instead of the real state).
 */

import { ApiError } from '../api/errors';

export function errorCodeOf(error: unknown): string | null {
  if (error instanceof ApiError) return error.shape.code;
  if (error instanceof Error && error.name === 'AbortError') return null;
  return error ? 'INTERNAL_001' : null;
}

export function errorMessageOf(error: unknown): string | null {
  if (error instanceof ApiError) return error.shape.message || null;
  if (error instanceof Error) return error.message || null;
  return null;
}
