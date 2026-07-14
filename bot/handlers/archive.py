"""Handlers for Archive Compress and Extract."""
from typing import Optional

from aiogram import Router, F
from aiogram.types import Message, CallbackQuery, FSInputFile
from aiogram.fsm.context import FSMContext

from bot.states.archive import ArchiveStates
from bot.keyboards.archive import (
    ARC_COMPRESS, ARC_EXTRACT, ARC_DONE, ARC_FORMAT_ZIP, ARC_FORMAT_7Z,
    archive_format_keyboard, archive_upload_done_keyboard,
)
from bot.keyboards.common import back_home_cancel
from core.constants import SUPPORTED_ARCHIVE_EXTS
from core.config import settings
from core.logger import logger

from services.archive.compressor import ArchiveCompressor, ArchiveFormat
from services.archive.extractor import ArchiveExtractor, MAX_EXTRACTED_FILES_TO_RETURN
from services.archive._common import ArchiveProcessingError
from services.security.validator import ArchiveSecurityError

from utils.tempfiles import new_temp_path, track_temp_file, untrack_temp_files, get_tracked_files, delete_paths, delete_path
from utils.validators import validate_extension

router = Router()


async def _track_usage(user_repo, db_user) -> None:
    if user_repo is not None and db_user is not None:
        try:
            await user_repo.increment_usage(db_user.telegram_id)
        except Exception:
            logger.exception("Failed to increment usage counter (non-fatal).")


async def _fail(message: Message, state: FSMContext, error: Exception, cleanup_paths: list) -> None:
    if isinstance(error, (ArchiveProcessingError, ArchiveSecurityError)):
        await message.answer(f"⚠️ {error}")
    else:
        logger.exception(f"Unexpected archive processing error: {error}")
        await message.answer("Something went wrong processing that archive. Please try again.")
    delete_paths(cleanup_paths)
    await untrack_temp_files(state, cleanup_paths)
    await state.clear()


# --------------------------------------------------------------------------
# Compress
# --------------------------------------------------------------------------

@router.callback_query(F.data == ARC_COMPRESS)
async def arc_compress_start(query: CallbackQuery, state: FSMContext):
    await state.set_state(ArchiveStates.waiting_for_compress_format)
    await query.message.edit_text("Choose the archive format to create:", reply_markup=archive_format_keyboard())
    await query.answer()


@router.callback_query(
    ArchiveStates.waiting_for_compress_format,
    F.data.in_({ARC_FORMAT_ZIP, ARC_FORMAT_7Z}),
)
async def arc_compress_format_chosen(query: CallbackQuery, state: FSMContext):
    fmt = ArchiveFormat.ZIP if query.data == ARC_FORMAT_ZIP else ArchiveFormat.SEVEN_Z
    await state.update_data(archive_format=fmt.value)
    await state.set_state(ArchiveStates.waiting_for_files_compress)
    await query.message.edit_text(
        f"Send the files to add to the {fmt.value.upper()} archive, one at a time.\n"
        f"Up to {settings.MAX_FILES_PER_BATCH} files. Press 'Done' when finished.",
        reply_markup=archive_upload_done_keyboard(),
    )
    await query.answer()


@router.message(ArchiveStates.waiting_for_files_compress, F.document)
async def arc_compress_receive(message: Message, state: FSMContext):
    current = await get_tracked_files(state)
    if len(current) >= settings.MAX_FILES_PER_BATCH:
        await message.answer(f"Maximum of {settings.MAX_FILES_PER_BATCH} files reached. Press 'Done'.")
        return

    doc = message.document
    if doc.file_size and doc.file_size > settings.MAX_FILE_SIZE:
        limit_mb = settings.MAX_FILE_SIZE // (1024 * 1024)
        await message.answer(f"File too large (max {limit_mb} MB).")
        return

    suffix = "." + (doc.file_name or "").rsplit(".", 1)[-1].lower() if "." in (doc.file_name or "") else ""
    # Preserve the original basename inside the archive for a nicer result,
    # while still writing to a collision-proof temp path on disk.
    temp_path = new_temp_path(suffix=suffix)
    await track_temp_file(state, temp_path)
    await message.bot.download(doc, destination=temp_path)

    names = (await state.get_data()).get("original_names", {})
    names[temp_path] = doc.file_name or f"file{suffix}"
    await state.update_data(original_names=names)

    count = len(await get_tracked_files(state))
    await message.answer(f"Added file {count}/{settings.MAX_FILES_PER_BATCH}. Send more or press 'Done'.")


@router.callback_query(ArchiveStates.waiting_for_files_compress, F.data == ARC_DONE)
async def arc_compress_done(query: CallbackQuery, state: FSMContext, user_repo=None, db_user=None):
    files = await get_tracked_files(state)
    await query.answer()
    if not files:
        await query.message.answer("Send at least one file first, or press Cancel.")
        return

    data = await state.get_data()
    fmt = ArchiveFormat(data.get("archive_format", ArchiveFormat.ZIP.value))

    await query.message.answer(f"Creating {fmt.value.upper()} archive... please wait.")
    try:
        output_path = await ArchiveCompressor().compress(files, fmt)
    except Exception as e:
        await _fail(query.message, state, e, files)
        return

    await _track_usage(user_repo, db_user)
    try:
        await query.message.answer_document(
            FSInputFile(output_path, filename=f"archive.{fmt.value}")
        )
    finally:
        all_paths = files + [output_path]
        delete_paths(all_paths)
        await untrack_temp_files(state, all_paths)
        await state.clear()


# --------------------------------------------------------------------------
# Extract
# --------------------------------------------------------------------------

@router.callback_query(F.data == ARC_EXTRACT)
async def arc_extract_start(query: CallbackQuery, state: FSMContext):
    await state.set_state(ArchiveStates.waiting_for_file_extract)
    await query.message.edit_text(
        "Send the archive to extract (.zip, .7z, or .rar).",
        reply_markup=back_home_cancel(),
    )
    await query.answer()


@router.message(ArchiveStates.waiting_for_file_extract, F.document)
async def arc_extract_process(message: Message, state: FSMContext, user_repo=None, db_user=None):
    doc = message.document
    if doc is None:
        await message.answer("Please send an archive file as a document.")
        return
    if doc.file_size and doc.file_size > settings.MAX_FILE_SIZE:
        limit_mb = settings.MAX_FILE_SIZE // (1024 * 1024)
        await message.answer(f"File too large (max {limit_mb} MB).")
        return
    if not validate_extension(doc.file_name or "", SUPPORTED_ARCHIVE_EXTS):
        allowed_str = ", ".join(sorted(SUPPORTED_ARCHIVE_EXTS))
        await message.answer(f"Unsupported file type. Allowed: {allowed_str}")
        return

    archive_type = (doc.file_name or "").rsplit(".", 1)[-1].lower()
    path = new_temp_path(suffix=f".{archive_type}")
    await track_temp_file(state, path)
    await message.bot.download(doc, destination=path)

    await message.answer("Extracting... please wait.")
    try:
        dest_dir, extracted = await ArchiveExtractor().extract(path, archive_type)
    except Exception as e:
        await _fail(message, state, e, [path])
        return

    await track_temp_file(state, dest_dir)

    await _track_usage(user_repo, db_user)
    cleanup_paths = [path, dest_dir]

    try:
        if len(extracted) > MAX_EXTRACTED_FILES_TO_RETURN:
            # Too many files to send individually -- re-zip them into one
            # convenience download instead of flooding the chat.
            await message.answer(
                f"Archive contains {len(extracted)} files (more than {MAX_EXTRACTED_FILES_TO_RETURN}); "
                f"sending them bundled back into one ZIP."
            )
            rezip_path = await ArchiveCompressor().compress(extracted, ArchiveFormat.ZIP)
            cleanup_paths.append(rezip_path)
            await message.answer_document(FSInputFile(rezip_path, filename="extracted.zip"))
        else:
            for f in extracted:
                import os
                await message.answer_document(FSInputFile(f, filename=os.path.basename(f)))
    finally:
        delete_paths(cleanup_paths)
        await untrack_temp_files(state, cleanup_paths)
        await state.clear()
