#pragma once

class class_model; // Forward declaration for use in warn_err
#include "class_lab.h"
#include "class_eq.h"
#include "class_part.h"
#include "class_oper.h"
#include "class_route.h"
#include "class_ibom.h"
#include "wused.h"
#include "oplst.h"

#include <direct.h>
#include <stdlib.h>
#include <stdio.h>
#include <iostream>
#include <fstream>
#include <cmath>

#define  BUFLEN    400
#define  NAME_SIZE  20
#define  MESSAGE_SIZE 400

#ifndef MAX
#define MAX(a,b) (((a)>(b))?(a):(b))
#endif

#ifndef MIN
#define MIN(a,b) (((a)<(b))?(a):(b))
#endif

#define sNULL "\0"

void warn_err(class_model* c1, char* buf1, int level,
	char* str_l, char* str_e, char* str_p,
	char* str_o, char* str_r, char* str_i);
class class_model
{

#define FALSE 0 
#define PERM -1

#define EQ_LIMIT 6000

public:  

	char varlocal [BUFLEN];

	int oplstcount;

	int CALC_CANCEL;  // true to cancel  false is  OK

	int opas_rt_err;
	char  op_error_name [MESSAGE_SIZE];
	int over_util_L;
	int over_util_E;
	int inOPT ;

    int WID;
    float    total_pro;
    float    total_shi;
    float    total_scr;
    float    total_wip;
    float    total_ft ;


	char nam1 [6];
	char nam2 [6];
	char nam3 [6];

	float t1;
	float t2;
	float utlimit;

	float v_part;
	float v_lab;
	float v_equip;

	int lab_point;
	int eq_point;
	int eq_point2;
	int part_point;
	int part_point2;
	int oper_point;
	int route_point;
	int ibom_point;

	int wused_point;
	int oplst_point;

	int lab_count;
	int eq_count;
	int part_count;
	int oper_count;
	int route_count;
	int ibom_count;

	int oplst_count;

	float  tot_gather;
	float  tot_weight;

	//  opt parmeters

	int numberOfParameterAffected;

	float * valueOfParameterAffected;   //  starts at   1
	class_part ** partPointer;            //  starts at 0
	int * isLotOrTbatch;                //  starts at 0

	float tolerance;
	int iter;
	float resultValue;

	int INRUN;
	int FULL;

	class_lab **   all_lab;
	class_eq  **   all_eq;
	class_part**   all_part;
	class_oper**   all_oper;
	class_route**  all_route;
	class_ibom**   all_ibom;

	class_wused **  all_wused;

	class_oplst **  all_oplst;

	 char * maxname;

	int ncom;  // move inside zsub ??

	float *pcom;
	float *xicom;

	// constructor/destructor declared in class_model.cpp
	class_model(int l_size, int e_size, int p_size, int o_size, int r_size, int i_size, char * dir1);
	~class_model(void);

	// methods used by dll2.cpp (many are inline in original)
	class_lab *first_lab_ptr();
	class_lab *next_lab_ptr();
	class_lab *xxadd_lab(char * name1, double size1);
	int find_lab_name( char *  lname1);
	class_lab * get_lab_num( int num);

	class_eq *first_eq_ptr();
	class_eq *next_eq_ptr();
	class_eq *first_eq_ptr2();
	class_eq *next_eq_ptr2();
	class_eq *xxadd_eq(char * name1, double size1);
	int find_eq_name( char * lname1);
	class_eq * get_eq_num( int num);

	class_part *first_part_ptr();
	class_part *next_part_ptr();
	class_part * first_part_ptr2 (void);
	class_part * next_part_ptr2 (void);
	class_part *add_part(char * name1, double size1);
	int find_part_name( char * lname1);
	class_part * get_part_num( int num);

	class_oper *first_oper_ptr();
	class_oper *next_oper_ptr();
	class_oper *add_oper(char * name1, int opnum);
	int find_oper_name( char * lname1);
	class_oper * get_oper_num( int num);

	class_route *first_route_ptr();
	class_route *next_route_ptr();
	class_route *add_route( double cmmpij);
	class_route * get_route_num( int num);

	class_ibom *first_ibom_ptr();
	class_ibom *next_ibom_ptr();
	class_ibom *add_ibom(char * name1, double upa);
	class_ibom * get_ibom_num( int num);

	void clean_wused();
	class_wused * add_wused(void);

	void clear_model_batch(char * dir1);

};

