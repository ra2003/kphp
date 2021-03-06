// Compiler for PHP (aka KPHP)
// Copyright (c) 2020 LLC «V Kontakte»
// Distributed under the GPL v3 License, see LICENSE.notice.txt

#include "runtime/php_assert.h"

#include <algorithm>
#include <cstdarg>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <ctime>
#include <cxxabi.h>
#include <execinfo.h>
#include <unistd.h>
#include <wait.h>

#include "common/fast-backtrace.h"

#include "runtime/critical_section.h"
#include "runtime/datetime.h"
#include "runtime/kphp-backtrace.h"
#include "runtime/misc.h"
#include "runtime/on_kphp_warning_callback.h"
#include "runtime/resumable.h"
#include "server/php-engine-vars.h"

const char *engine_tag = "[";
const char *engine_pid = "] ";
int release_version = 0;

int php_disable_warnings = 0;
int php_warning_level = 2;
int php_warning_minimum_level = 0;

extern FILE* json_log_file_ptr;

// linker magic: run_scheduler function is declared in separate section.
// their addresses could be used to check if address is inside run_scheduler
struct nothing {};
extern nothing __start_run_scheduler_section;
extern nothing __stop_run_scheduler_section;
static bool is_address_inside_run_scheduler(void *address) {
  return &__start_run_scheduler_section <= address && address <= &__stop_run_scheduler_section;
};

void write_json_error_to_log(int version, char *msg, int type, int nptrs, void** buffer);

static void print_demangled_adresses(void **buffer, int nptrs, int num_shift, bool allow_gdb) {
  if (php_warning_level == 1) {
    for (int i = 0; i < nptrs; i++) {
      fprintf(stderr, "%p\n", buffer[i]);
    }
  } else if (php_warning_level == 2) {
    KphpBacktrace demangler{buffer, nptrs};
    int32_t index = num_shift;
    auto demangled_range  = demangler.make_demangled_backtrace_range(true);
    for (const char *line : demangled_range) {
      if (line) {
        fprintf(stderr, "(%d) %s", index++, line);
      }
    }
    if (index == num_shift) {
      backtrace_symbols_fd(buffer, nptrs, 2);
    }
  } else if (php_warning_level == 3 && allow_gdb) {
    char pid_buf[30];
    sprintf(pid_buf, "%d", getpid());
    char name_buf[512];
    ssize_t res = readlink("/proc/self/exe", name_buf, 511);
    if (res >= 0) {
      name_buf[res] = 0;
      int child_pid = fork();
      if (!child_pid) {
        dup2(2, 1); //redirect output to stderr
        execlp("gdb", "gdb", "--batch", "-n", "-ex", "thread", "-ex", "bt", name_buf, pid_buf, nullptr);
        fprintf(stderr, "Can't print backtrace with gdb: gdb failed to start\n");
      } else {
        if (child_pid > 0) {
          waitpid(child_pid, nullptr, 0);
        } else {
          fprintf(stderr, "Can't print backtrace with gdb: fork failed\n");
        }
      }
    } else {
      fprintf(stderr, "Can't print backtrace with gdb: can't get name of executable file\n");
    }
  }
}

static void php_warning_impl(bool out_of_memory, int error_type, char const *message, va_list args) {
  if (php_warning_level == 0 || php_disable_warnings) {
    return;
  }

  const auto malloc_replacer_rollback = temporary_rollback_malloc_replacement();

  static const int BUF_SIZE = 1000;
  static char buf[BUF_SIZE];
  static const int warnings_time_period = 300;
  static const int warnings_time_limit = 1000;

  static int warnings_printed = 0;
  static int warnings_count_time = 0;
  static int skipped = 0;
  int cur_time = (int)time(nullptr);

  if (cur_time >= warnings_count_time + warnings_time_period) {
    warnings_printed = 0;
    warnings_count_time = cur_time;
    if (skipped > 0) {
      fprintf(stderr, "[time=%d] Resuming writing warnings: %d skipped\n", (int)time(nullptr), skipped);
      skipped = 0;
    }
  }

  if (++warnings_printed >= warnings_time_limit) {
    if (warnings_printed == warnings_time_limit) {
      fprintf(stderr, "[time=%d] Warnings limit reached. No more will be printed till %d\n", cur_time, warnings_count_time + warnings_time_period);
    }
    ++skipped;
    return;
  }

  const bool allocations_allowed = !out_of_memory && !dl::in_critical_section;
  dl::enter_critical_section();//OK

  fprintf(stderr, "%s%d%sWarning: ", engine_tag, cur_time, engine_pid);
  vsnprintf(buf, BUF_SIZE, message, args);
  fprintf(stderr, "%s\n", buf);

  bool need_stacktrace = php_warning_level >= 1;
  int nptrs = 0;
  void *buffer[64];
  if (need_stacktrace) {
    fprintf(stderr, "------- Stack Backtrace -------\n");
    nptrs = fast_backtrace(buffer, sizeof(buffer) / sizeof(buffer[0]));
    if (php_warning_level == 1) {
      nptrs -= 2;
      if (nptrs < 0) {
        nptrs = 0;
      }
    }

    int scheduler_id = static_cast<int>(std::find_if(buffer, buffer + nptrs, is_address_inside_run_scheduler) - buffer);
    if (scheduler_id == nptrs) {
      print_demangled_adresses(buffer, nptrs, 0, true);
    } else {
      print_demangled_adresses(buffer, scheduler_id, 0, true);
      void *buffer2[64];
      int res_ptrs = get_resumable_stack(buffer2, sizeof(buffer2) / sizeof(buffer2[0]));
      print_demangled_adresses(buffer2, res_ptrs, scheduler_id, false);
      print_demangled_adresses(buffer + scheduler_id, nptrs - scheduler_id, scheduler_id + res_ptrs, false);
    }

    fprintf(stderr, "-------------------------------\n\n");
  }

  dl::leave_critical_section();
  if (allocations_allowed) {
    OnKphpWarningCallback::get().invoke_callback(string(buf));
  }

  if (need_stacktrace && json_log_file_ptr != nullptr) {
    write_json_error_to_log(release_version, buf, error_type, nptrs, buffer);
  }

  if (die_on_fail) {
    raise(SIGPHPASSERT);
    fprintf(stderr, "_exiting in php_warning, since such option is enabled\n");
    _exit(1);
  }
}

void php_notice(char const *message, ...) {
  va_list args;
  va_start (args, message);
  php_warning_impl(false, E_NOTICE, message, args);
  va_end(args);
}

void php_warning(char const *message, ...) {
  va_list args;
  va_start (args, message);
  php_warning_impl(false, E_WARNING, message, args);
  va_end(args);
}

void php_error(char const *message, ...) {
  va_list args;
  va_start (args, message);
  php_warning_impl(false, E_ERROR, message, args);
  va_end(args);
}

void php_out_of_memory_warning(char const *message, ...) {
  va_list args;
  va_start (args, message);
  php_warning_impl(true, E_ERROR, message, args);
  va_end(args);
}

void php_assert__(const char *msg, const char *file, int line) {
  php_error("Assertion \"%s\" failed in file %s on line %d", msg, file, line);
  raise(SIGPHPASSERT);
  fprintf(stderr, "_exiting in php_assert\n");
  _exit(1);
}

void raise_php_assert_signal__() {
  raise(SIGPHPASSERT);
}

// type is one of the error E_* constants (E_ERROR, E_WARNING, etc.)
void write_json_error_to_log(int version, char *msg, int type, int nptrs, void** buffer) {
  for (char *c = msg; *c; ++c) {
    if (*c == '"') {
      *c = '\'';
    } else if (*c == '\n') {
      *c = ' ';
    }
  }

  const auto &err_context = KphpErrorContext::get();

  const auto format = R"({"version":%d,"type":%d,"created_at":%ld,"msg":"%s","env":"%s")";
  fprintf(json_log_file_ptr, format, version, type, time(nullptr), msg, err_context.env_c_str());

  fprintf(json_log_file_ptr, R"(,"trace":[)");
  for (int i = 0; i < nptrs; i++) {
    if (i != 0) {
      fprintf(json_log_file_ptr, ",");
    }
    fprintf(json_log_file_ptr, R"("%p")", buffer[i]);
  }
  fprintf(json_log_file_ptr, "]");

  if (err_context.tags_are_set()) {
    fprintf(json_log_file_ptr, R"(,"tags":%s)", err_context.tags_c_str());
  }

  if (err_context.extra_info_is_set()) {
    fprintf(json_log_file_ptr, R"(,"extra_info":%s)", err_context.extra_info_c_str());
  }

  fprintf(json_log_file_ptr, "}\n");
  fflush(json_log_file_ptr);
}

KphpErrorContext &KphpErrorContext::get() {
  static KphpErrorContext context;
  return context;
}

void KphpErrorContext::set_tags(const char *ptr, size_t size) {
  if ((size + 1) > sizeof(tags_buffer)) {
    return;
  }

  memcpy(tags_buffer, ptr, size);
  tags_buffer[size] = '\0';
}

void KphpErrorContext::set_extra_info(const char *ptr, size_t size) {
  if ((size + 1) > sizeof(extra_info_buffer)) {
    return;
  }

  memcpy(extra_info_buffer, ptr, size);
  extra_info_buffer[size] = '\0';
}

void KphpErrorContext::set_env(const char *ptr, size_t size) {
  if ((size + 1) > sizeof(env_buffer)) {
    return;
  }

  memcpy(env_buffer, ptr, size);
  env_buffer[size] = '\0';
}

void KphpErrorContext::reset() {
  extra_info_buffer[0] = '\0';
  tags_buffer[0] = '\0';
  env_buffer[0] = '\0';
}
