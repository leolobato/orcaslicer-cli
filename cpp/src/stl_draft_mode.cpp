#include "stl_draft_mode.h"

#include <string>

namespace orca_headless {

namespace {

bool is_supported_operation_name(const std::string& operation) {
    return operation == "import"
        || operation == "layout"
        || operation == "export_3mf";
}

int fail(const std::string& code, const std::string& message, StlDraftResponse& r) {
    r.status = "error";
    r.error_code = code;
    r.error_message = message;
    write_stl_draft_response_to_stdout(r);
    return 1;
}

}  // namespace

int run_stl_draft_mode(const StlDraftRequest& req) {
    StlDraftResponse response;
    if (req.operation.empty()) {
        return fail("invalid_request", "operation is required", response);
    }
    if (!is_supported_operation_name(req.operation)) {
        return fail("invalid_request", "operation must be one of: import, layout, export_3mf", response);
    }
    return fail("unsupported_operation", "stl-draft operation is unavailable in this build", response);
}

}  // namespace orca_headless
