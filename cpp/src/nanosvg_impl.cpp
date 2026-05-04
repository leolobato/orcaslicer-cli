// nanosvg + nanosvgrast are header-only libraries. The library's
// implementation symbols are emitted only when one TU in the link defines
// NANOSVG_IMPLEMENTATION / NANOSVGRAST_IMPLEMENTATION before including
// the headers.
//
// Upstream OrcaSlicer relies on `src/slic3r/GUI/BitmapCache.cpp` to emit
// these. With SLIC3R_GUI=OFF the GUI is excluded from compilation, but
// libslic3r itself still references `nsvgParseFromFile` / `nsvgDelete` /
// `nsvgParse` (in `NSVGUtils.cpp`, `Format/svg.cpp`). This TU restores
// those symbols for our headless build.

#define NANOSVG_IMPLEMENTATION
#define NANOSVGRAST_IMPLEMENTATION
#include "nanosvg.h"
#include "nanosvgrast.h"
