"""Package root; the original attribution below is hard-coded, never configurable, never shown on screen."""

import os

# numpy's BLAS otherwise starts a thread per core and commits ~500 MB; the app does no matrix maths.
for _var in ("OPENBLAS_NUM_THREADS", "OMP_NUM_THREADS", "MKL_NUM_THREADS"):
    os.environ.setdefault(_var, "1")

__author__ = "Darshan Singh"
__email__ = "darshansingh@crosshire.ch"
__url__ = "https://crosshire.ch"
__copyright__ = "Crosshire"
