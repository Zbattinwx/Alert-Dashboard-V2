"""Process-wide eccodes decode lock.

eccodes is NOT thread-safe: its .def parser (flex) keeps global buffer state,
so concurrent decodes (codes_new_from_message / codes_grib_new_from_file) from
different threads corrupt it ("end of buffer missed" / template syntax errors).
The HRRR field prefetcher, the MRMS poll loop, the MRMS rotation poll loop, and
asyncio.to_thread request handlers all decode GRIB on independent threads, so
every module that calls eccodes must hold this ONE shared lock around the
decode section (keep the slow S3 downloads outside it — only the eccodes calls
need serializing).
"""

import threading

GRIB_DECODE_LOCK = threading.Lock()
