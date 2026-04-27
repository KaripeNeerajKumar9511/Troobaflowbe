#pragma once

#include <stdio.h>

// forward declaration
class class_model;
class class_route;
class class_oper;
class class_part;

void write_check(char* varlocal, char* s0);
void write_check2(char* varlocal, char* s0);
void read_check(char* xx1);
void warn_err(class_model* c1, char* buf1, int level,
    char* str_l, char* str_e, char* str_p,
    char* str_o, char* str_r, char* str_i);

// Add missing function declarations
class_route* find_rt_from(class_model* c1, const char* op_name, class_part* tpart);
class_oper* find_opas(class_model* c1, const char* op_name, class_part* tpart);
