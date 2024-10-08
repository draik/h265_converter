"""Defines all of the jobs and shared functions."""

import datetime
import logging
import os
import sqlite3
import subprocess
from pathlib import Path

from ffmpeg import FFmpeg, FFmpegError, Progress

from h265_transcoder import config
from h265_transcoder.interfaces import DatabaseInterface

logger = logging.getLogger("app")
BATCH = os.getenv("BATCH", "0")
DELETE = bool(os.environ["DELETE"].lower() == "true")


class Transcode:
    """Instantiate the video file for transcoding."""
    def __init__(self, sqlite_db: str, path: str, filename: str) -> None:
        """Setup the path and filename instance for video transcoding.

        Args:
            sqlite_db (str): SQLite database file to use.
            path (str): the absolute path to the video file.
            filename (str): the video filename.
        """
        self.sqlite_db = sqlite_db
        self.path = path
        self.filename = filename
        self.input_file = f"{self.path}/{self.filename}"
        if self.filename.endswith(".mkv"):
            self.output_file = self.input_file.replace(".mkv", ".mp4")
            self.video_title = self.filename.removesuffix(".mkv")
        elif self.filename.endswith(".mp4"):
            self.output_file = self.input_file.replace(".mp4", ".h265")
            self.video_title = self.filename.removesuffix(".mp4")


    def transcode(self) -> str:
        """Transcode the video file to h.265 HVC1 MP4.

        During the transcoding, metadata will be cleaned up.
        Title tag will match the filename without an extension.
        Comment tag will be cleared.

        Returns:
            transcode_status: "done" for success, "failed" for errors.
        """
        update_status(self.sqlite_db, self.path, self.filename, "active")
        ffmpeg = (
            FFmpeg()
            .option("y")
            .input(self.input_file)
            .output(
                self.output_file,
                {
                    "codec:v": "libx265",
                    "vtag": "hvc1",
                    "codec:a": "copy",
                    "metadata": [
                                    f"title={self.video_title}",
                                    "comment="
                                 ],
                    "f": "mp4"
                }
            )
        )

        @ffmpeg.on("start")
        def on_start(command: list[str]):
            ffmpeg_cmd_msg = f"{command=}"
            logger.debug(ffmpeg_cmd_msg)

        @ffmpeg.on("progress")
        def on_progress(progress: Progress):
            frame = progress.frame
            fps = int(progress.fps)
            size = (str(progress.size) + "B")
            try:
                seconds = datetime.datetime.strptime(str(progress.time), "%H:%M:%S.%f")
            except ValueError:
                seconds = datetime.datetime.strptime(str(progress.time), "%H:%M:%S")
            time = seconds.strftime("%H:%M:%S.") + str(seconds.strftime("%f"))[:2]
            bitrate = str(progress.bitrate) + "kb/s"
            speed = (str(progress.speed) + "x")
            progress_bar = (
                f"File={self.filename} "
                f"Frame={frame} "
                f"FPS={fps} "
                f"Size={size} "
                f"Time={time} "
                f"Bitrate={bitrate} "
                f"Speed={speed}"
            )
            logger.transcode(progress_bar)

        try:
            transcode_msg = f"Transcoding '{self.input_file}' to '{self.output_file}'."
            logger.info(transcode_msg)
            ffmpeg.execute()
        except FFmpegError:
            transcode_status = "failed"
            transcode_err_msg = f"Failed to transcode '{self.input_file}'"
            logger.error(transcode_err_msg)
            if Path(self.output_file).exists():
                logger.debug("Removing the failed output file.")
                Path(self.output_file).unlink()
                logger.debug("Removed output file.")
            else:
                cleanup_msg = f"Nothing to remove. '{self.output_file}' not found."
                logger.debug(cleanup_msg)
        else:
            transcode_status = "done"
            success_msg = f"'{self.input_file}' transcoded successfully."
            logger.info(success_msg)
            input_size = get_file_size(self.input_file)
            output_size = get_file_size(self.output_file)
            diff_size = input_size-output_size
            diff_size_msg = f"Recovered {diff_size:,} bytes in trandcoding."
            logger.info(diff_size_msg)
        finally:
            update_status(self.sqlite_db, self.path, self.filename, transcode_status)
        return transcode_status


    def delete_original(self) -> None:
        """Remove the original input file.

        MKV transcoding outputs to MP4 file, and the original MKV will be deleted.
        MP4 transcoding outputs to ".h265" MP4, which will overwrite the ".mp4" file.
        """
        if self.input_file.endswith(".mkv"):
            Path(self.input_file).unlink()
            cleanup_msg = f"Deleted '{self.input_file}'."
        elif self.input_file.endswith(".mp4"):
            Path(self.output_file).replace(self.input_file)
            cleanup_msg = f"Renamed '{self.output_file}' to '{self.input_file}'."
        logger.info(cleanup_msg)


def final_results(sqlite_db: str) -> None:
    """Get the final count per status, and filenames with a failed status.

    Args:
        sqlite_db (str): SQLite database file to use.
    """
    states = {
        "done": 0,
        "failed": 0,
        "queued": 0,
        "skipped": 0,
        "unknown": 0
    }
    status_query = "SELECT status, COUNT(status) FROM queue GROUP BY status ;"
    failed_query = "SELECT path, filename FROM queue WHERE status = 'failed' ;"
    with DatabaseInterface(sqlite_db) as (_connect, db_cursor):
        try:
            status_result = db_cursor.execute(status_query)
            status_data = status_result.fetchall()
            failed_result = db_cursor.execute(failed_query)
            failed_data = failed_result.fetchall()
        except sqlite3.Error:
            logger.error("SQLite status query failed.")
            logger.exception(sqlite3.Error)
        else:
            db_cursor.close()

    for values in status_data:
        states[values[0]] = values[1]

    final_count_msg = (f"{states["done"]} done, {states["failed"]} failed, "
                       f"{states["queued"]} queued, {states["skipped"]} skipped, "
                       f"{states['unknown']} unknown.")
    logger.info(final_count_msg)

    if failed_data:
        for path, filename in failed_data:
            failed_file = f"{path}/{filename}"
            failed_result_msg = f"Failed to transcode '{failed_file}'."
            logger.info(failed_result_msg)


def get_batch(sqlite_db: str) -> list:
    """Obtain a list of files to transcode based on batch limit.

    Args:
        sqlite_db (str): SQLite database file to use.

    Returns:
        List of tuples containing the '(path, filename)' of files to transcode.
    """
    try:
        batch = int(BATCH)
    except ValueError:
        value_error_msg = f"BATCH is not an integer. {BATCH=}."
        logger.error(value_error_msg)
        logger.info("Setting batch to unlimited.")
        limit = None
    else:
        if batch == 0:
            logger.info("Batch is 0; unlimited.")
            limit = None
        elif batch > 0:
            batch_msg = f"Setting batch limit to {batch}."
            logger.info(batch_msg)
            limit = batch
        elif batch < 0:
            batch_msg = f"{batch=}. BATCH variable must be a positive number."
            logger.warning(batch_msg)
            negative_msg = f"Batch is '{batch}'. Setting to unlimited."
            logger.info(negative_msg)
            limit = None
        else:
            limit = None

    batch_query = "SELECT path, filename FROM queue WHERE transcode = 'Y' AND status = 'queued' ;"
    if limit:
        batch_query = batch_query.replace(";", f"LIMIT {limit} ;")
    with DatabaseInterface(sqlite_db) as (_connect, db_cursor):
        try:
            batch_result = db_cursor.execute(batch_query)
            batch_queue = batch_result.fetchall()
        except sqlite3.Error:
            logger.error("SQLite transcode selection query failed.")
            logger.exception(sqlite3.Error)
            raise SystemExit(1) from sqlite3.Error
        else:
            db_cursor.close()
            logger.info("Successfully retrieved batch of files to transcode.")
            return batch_queue


def get_file_size(filename: str) -> int:
    """Get the file size of the input and output file.

    Args:
        filename (str): file to get its size
    Returns:
        int of size in bytes
    """
    byte_size = Path(filename).stat().st_size
    gigabyte = 1073741824
    megabyte = 1048576

    if byte_size >= gigabyte:
        human_size = int(byte_size/(1024*1024*1024))
        human_size_msg = f"'{filename}' is {human_size}GB."
    elif byte_size >= megabyte:
        human_size = int(byte_size/(1024*1024))
        human_size_msg = f"'{filename}' is {human_size}MB."
    else:
        human_size_msg = f"'{filename}' is {byte_size:,}B."
    logger.info(human_size_msg)
    return byte_size


def insert_scan_results(sqlite_db: str, insert_list: list) -> None:
    """Insert scan results list into SQLite database.

    Args:
        sqlite_db (str): SQLite database file to use.
        insert_list (list): list containing a list of scan results.
    """
    insert_statement = """INSERT INTO queue (
                                path, filename, transcode, status)
                            VALUES (
                                ?, ?, ?, ?) ;
    """

    logger.debug("Inserting scanned results into SQLite database.")

    with DatabaseInterface(sqlite_db) as (_connect, db_cursor):
        try:
            db_cursor.executemany(insert_statement, insert_list)
        except sqlite3.IntegrityError:
            logger.error("Duplicate filename found in SQLite table.")
        except sqlite3.Error:
            logger.error("SQLite insert execution failed.")
            logger.exception(sqlite3.Error)
            raise SystemExit(1) from sqlite3.Error
        else:
            db_cursor.close()
        finally:
            insert_msg = f"Successfully inserted {len(insert_list)} entries into SQLite 'queue' table."
            logger.info(insert_msg)


def read_metadata(path: str, filename: str) -> tuple:
    """Read video file metadata for Compressor ID.

    Args:
        path (str): Absolute path to the video file.
        filename (str): Filename for the video.

    Returns:
        Tuple of the filename and transcoding status.
    """
    video_file = f"{path}/{filename}"
    try:
        reader_cmd = ["/usr/bin/exiftool",
                      "-api", "largefilesupport",
                      "-s3", "-CompressorID",
                      video_file]
        metadata_sp = subprocess.run(reader_cmd,
                                        capture_output = True,
                                        check = True,
                                        text = True)
        compressor_metadata = metadata_sp.stdout.lower().strip()
    except subprocess.CalledProcessError:
        non_video_msg = f"'{video_file}' is not a not a video file. Verify file type."
        logger.error(non_video_msg)
        result = (filename, "N", "unknown")
    else:
        if compressor_metadata == "hvc1":
            transcoded_msg = f"'{video_file}' is already transcoded."
            logger.info(transcoded_msg)
            result = (filename, "N", "skipped")
        elif compressor_metadata == "":
            unknown_msg = f"'{video_file}' returned empty Compressor ID. Verifying video integrity."
            logger.warning(unknown_msg)
            verified_status = verify_metadata(video_file)
            result = (filename, *verified_status)
        else:
            transcode_msg = f"'{video_file}' needs to be transcoded."
            logger.info(transcode_msg)
            result = (filename, "Y", "queued")
    return result


def retry_failed(sqlite_db: str) -> list:
    """Retry transcoding files that failed transcoding on the first attempt.

    Args:
        sqlite_db (str): SQLite database file to use.

    Returns:
        list of tuples containing the path and filename of failed transcoding.
    """
    failed_status_query = "SELECT path, filename FROM queue WHERE status = 'failed';"

    with DatabaseInterface(sqlite_db) as (_connect, db_cursor):
        try:
            failed_status_result = db_cursor.execute(failed_status_query)
            failed_status_data = failed_status_result.fetchall()
        except sqlite3.Error:
            logger.error("SQLite status query failed.")
            logger.exception(sqlite3.Error)
        else:
            db_cursor.close()

    if failed_status_data:
        failed_results_msg = f"Found {len(failed_status_data)} failed transcoding(s)."
        logger.info(failed_results_msg)
    else:
        logger.info("No failed transcodings.")

    return failed_status_data


def scan_directory(sqlite_db: str) -> None:
    """Scan for video files.

    Creates a tuple of the absolute path and filename,
    then appends to the list of the scan results.

    Args:
        sqlite_db (str): SQLite database file to use.

    Returns:
        List of tuples containing the absolute path and filename.
    """
    scan_path = "/mnt"
    video_extensions = (".mkv", ".mp4")
    video_list = []
    queue_list = []

    logger.info("Beginning scan...")
    for root, _dirs, files in os.walk(scan_path):
        for filename in files:
            if filename.endswith(video_extensions):
                video_list.append((root, filename))
                found_msg = f"Found '{root}/{filename}'."
                logger.info(found_msg)
    scan_results_msg = f"Scan complete. Found {len(video_list)} video file(s)."
    logger.info(scan_results_msg)

    if len(video_list) == 0:
        logger.warning("Empty scan results. Is the volume mounted? Exiting.")
        raise SystemExit(1)

    logger.debug("Checking metadata on video files.")
    for result in video_list:
        path = result[0]
        filename = result[1]
        if filename.endswith(".mkv"):
            transcode_msg = f"'{path}/{filename}' needs to be transcoded."
            logger.info(transcode_msg)
            queue_list.append([path, filename, "Y", "queued"])
        else:
            filename, transcode, status = read_metadata(path, filename)
            queue_list.append([path, filename, transcode, status])
    insert_scan_results(sqlite_db, queue_list)


def setup_database(sqlite_db: str) -> int:
    """Setup the SQLite database.

    Args:
        sqlite_db (str): SQLite database file to use.

    Returns:
        0 - successfully import schema_file into SQLite DB.
        Any non-zero value is a failure to import the schema_file.
    """
    schema_file = config.schema_file
    try:
        with Path(schema_file).open(mode="r", encoding="utf-8") as db_schema:
            create_table = db_schema.read()
    except FileNotFoundError:
        file_not_found_msg = f"Schema file not found. Expected: '{schema_file}'."
        logger.error(file_not_found_msg)
        raise SystemExit(1) from FileNotFoundError
    else:
        with DatabaseInterface(sqlite_db) as (connection, cursor):
            cursor.executescript(create_table)
            cursor.close()
            logger.debug("SQLite database is ready.")
        return 0


def transcode_queue(sqlite_db: str, queue_list: list) -> None:
    """Transcode the list of files.

    Args:
        sqlite_db (str): SQLite database file to use.
        queue_list (list): list of tuples containing a path and filename.
    """
    for entry in queue_list:
        path = entry[0]
        filename = entry[1]
        video_file = Transcode(sqlite_db, path, filename)
        transcode_video = video_file.transcode()
        if (transcode_video == "done") and (DELETE):
            video_file.delete_original()


def update_metadata(sqlite_db: str) -> None:
    """Update the metadata of the video file.

    Args:
        sqlite_db (str): SQLite database file to use.
    """
    metadata_query = "SELECT path, filename FROM queue ;"
    with DatabaseInterface(sqlite_db) as (_connect, db_cursor):
        try:
            metadata_result = db_cursor.execute(metadata_query)
            metadata_queue = metadata_result.fetchall()
        except sqlite3.Error:
            logger.error("SQLite metadata query failed.")
            logger.exception(sqlite3.Error)
            raise SystemExit(1) from sqlite3.Error
        else:
            db_cursor.close()
            logger.debug("Successfully retrieved list of files to update metadata.")

    for file in metadata_queue:
        path = file[0]
        filename = file[1]
        video_file = f"{path}/{filename}"
        if filename.endswith(".mp4"):
            try:
                video_title = filename.removesuffix(".mp4")
                update_metadata_cmd = ["/usr/bin/exiftool",
                                        "-overwrite_original",
                                        f"-title={video_title}",
                                        "-comment=",
                                        video_file]
                subprocess.run(update_metadata_cmd,
                                capture_output = True,
                                check = True,
                                text = True)
            except subprocess.CalledProcessError:
                update_metadata_err = f"Invalid MP4 file type for '{video_file}'. Transcode to update the metadata."
                logger.error(update_metadata_err)
            else:
                update_metadata_msg = f"Updated metadata for '{video_file}'."
                logger.info(update_metadata_msg)
        else:
            file_type_warn = f"'{video_file}' is not MP4. Transcode to update the metadata."
            logger.warning(file_type_warn)


def update_status(sqlite_db: str, path: str, filename: str, status: str) -> None:
    """Update the status of the video file.

    Args:
        sqlite_db (str): SQLite database file to use.
        path (str): path for the video file.
        filename (str): filename for the video file.
        status (str): new status for the video file.
    """
    status_update_query = """UPDATE queue
                                SET status = ?
                                WHERE path = ? AND
                                filename = ? ;
    """
    status_update_data = (status, path, filename)
    with DatabaseInterface(sqlite_db) as (_connect, db_cursor):
        try:
            db_cursor.execute(status_update_query, status_update_data)
        except sqlite3.Error:
            update_query_msg = f"{status_update_data=}"
            logger.debug(update_query_msg)
            logger.error("SQLite transcode status update failed.")
            logger.exception(sqlite3.Error)
        else:
            db_cursor.close()
            sql_update_msg = f"Updated status for '{path}/{filename}' to '{status}'."
            logger.info(sql_update_msg)


def verify_database() -> int:
    """Verify the persistent database has a table with data.

    Return:
        queue_count (int): a count of the rows in the persistent database.
    """
    sqlite_db = config.persist_db
    setup_database(sqlite_db)

    queue_count = 0
    query_queue = "SELECT COUNT(*) FROM queue ;"
    with DatabaseInterface(sqlite_db) as (_connect, db_cursor):
        try:
            queue_result = db_cursor.execute(query_queue)
        except sqlite3.Error:
            verification_err_msg = f"SQLite database verification failed for '{sqlite_db}'."
            logger.error(verification_err_msg)
            raise SystemExit(1) from sqlite3.Error
        else:
            result = queue_result.fetchall()[0][0]
            queue_count = result if int(result) else 0
        finally:
            db_cursor.close()
    return queue_count


def verify_metadata(filename: str) -> tuple:
    """Verify the metadata for an invalid video file type.

    Args:
        filename (str): video file to check its metadata.

    Returns:
        A tuple containing the transcode and queue status values.
    """
    file_type_cmd = ["/usr/bin/exiftool",
                     "-s3", "-DocType",
                     filename]
    file_type_sp = subprocess.run(file_type_cmd,
                                    capture_output = True,
                                    check = True,
                                    text = True)
    file_type = file_type_sp.stdout.lower().strip()
    if file_type == "matroska":
        filetype_mkv_msg = f"'{filename}' is MKV file type, not MP4. Queued for transcoding."
        logger.warning(filetype_mkv_msg)
        transcode_status = ("Y", "queued")
    else:
        filetype_unknown_msg = f"'{filename}' is '{file_type}' type. Status is unknown."
        logger.error(filetype_unknown_msg)
        transcode_status = ("N", "unknown")
    return transcode_status
