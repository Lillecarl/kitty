//========================================================================
// GLFW 3.4 - www.glfw.org
//------------------------------------------------------------------------
// Copyright (c) 2016 Google Inc.
// Copyright (c) 2016-2017 Camilla Löwy <elmindreda@glfw.org>
//
// This software is provided 'as-is', without any express or implied
// warranty. In no event will the authors be held liable for any damages
// arising from the use of this software.
//
// Permission is granted to anyone to use this software for any purpose,
// including commercial applications, and to alter it and redistribute it
// freely, subject to the following restrictions:
//
// 1. The origin of this software must not be misrepresented; you must not
//    claim that you wrote the original software. If you use this software
//    in a product, an acknowledgment in the product documentation would
//    be appreciated but is not required.
//
// 2. Altered source versions must be plainly marked as such, and must not
//    be misrepresented as being the original software.
//
// 3. This notice may not be removed or altered from any source
//    distribution.
//
//========================================================================
// It is fine to use C99 in this file because it will not be built with VS
//========================================================================

#include "internal.h"

#include <stdlib.h>


//////////////////////////////////////////////////////////////////////////
//////                       GLFW platform API                      //////
//////////////////////////////////////////////////////////////////////////

int
_glfwPlatformInit(bool *supports_window_occlusion) {
    *supports_window_occlusion = false;
    _glfwPollMonitorsNull();
    // There is no display connection, so the poll set holds only the wakeup
    // and timer machinery. poll() ignores a negative fd, which is what the
    // display slot stays at for the lifetime of this backend.
    if (!initPollData(&_glfw.null.eventLoopData, -1)) {
        _glfwInputError(GLFW_PLATFORM_ERROR, "Null: Failed to initialize event loop data");
        return false;
    }

    return true;
}

void
_glfwPlatformTerminate(void) {
    free(_glfw.null.clipboardString);
    _glfw.null.clipboardString = NULL;
    finalizePollData(&_glfw.null.eventLoopData);
    _glfwTerminateOSMesa();
}

#define GLFW_LOOP_BACKEND null
#include "main_loop.h"

const char *
_glfwPlatformGetVersionString(void) {
    (void)keep_going;
    return _GLFW_VERSION_NUMBER " null headless";
}
