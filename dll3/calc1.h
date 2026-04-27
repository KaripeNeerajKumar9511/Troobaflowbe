#pragma once

#include "class_model.h"

// ================= MACROS =================
#define EPSILON       1e-6
#define SSEPSILON     1e-20

#define MAX(a,b)    ( (a>b) ? a : b )
#define MIN(a,b)    ( (a<b) ? a : b )

#define LABOR_T   1
#define EQUIP_T   2

#define LABOR_T_GATHER_1  27
#define LABOR_T_GATHER_2  28
#define LABOR_T_GATHER_3  6

#define EQUIP_T_GATHER_1  37
#define EQUIP_T_GATHER_2  38
#define EQUIP_T_GATHER_3  7

#define T_BATCH_TOTAL_LABOR  13
#define T_BATCH_TOTAL_EQUIP  14
#define T_BATCH_PIECE        23
#define T_BATCH_WAIT_LOT     24

#define LABOR_1P 47
#define EQUIP_1P 48
#define LABOR_1_TB 57
#define EQUIP_1_TB 58

#define LABOR_IJK 67
#define EQUIP_IJK 68

#define LABOR_DIF     101
#define EQUIP_DIF     102

#define LABOR_DIF_T   103
#define EQUIP_DIF_T   104

#define LABOR_DIF_1   105
#define EQUIP_DIF_1   106

// ================= FUNCTIONS =================

// core functions used in calc11.cpp
void do_gather(class_model*, class_part*, class_oper*, float, float);
float get_gather(class_model*, class_part*);
void calc_op(class_model*, class_part*, class_oper*, float*, float*, float*, int);
float calc_xprime(class_model*, class_part*, class_oper*, float, float);
double effabs(class_model*, class_lab*);

// optional (if used elsewhere)
int mpc(class_model*);
void getx(class_eq*);

