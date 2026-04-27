#include <iostream>

extern "C" int irun_it(
    char* dir1,
    float time1, float time2, float u_limit,
    float lab_var, float eq_var, float part_var,

    int l_count, int* x1, float* x2, float* x3, float* x4,
    float* x5, float* x6, float* x7, int* x8,

    int eq_count, int* e_x1, int* e_x2, float* e_x3, float* e_x4, float* e_x5,
    int* e_x6, float* e_x7, float* e_x8, float* e_x9, int* e_x10,

    int p_count, int* p_x1, float* p_x2, float* p_x3, float* p_x4,
    float* p_x5, float* p_x6, int* p_x7,

    int op_count, int* o_x1, int* o_x2, int* o_x3, int* o_x4, float* o_x5,
    float* o_x6, float* o_x7, float* o_x8, float* o_x9,
    float* o_x10, float* o_x11, float* o_x12, float* o_x13,
    float* o_x14, float* o_x15, float* o_x16, float* o_x17,

    int r_count, int* r_x1, int* r_x2, int* r_x3, float* r_x4,

    int ib_count, int* i_x1, int* i_x2, float* i_x3,

    int iWID
);

int main() {

    char dir[] = "C:\\temp";  // make sure this folder exists

    // ---------------- LABOR ----------------
    int l_count = 1;
    int x1[1] = { 1 };
    float x2[1] = { 1 };  // num
    float x3[1] = { 0 };  // ovt
    float x4[1] = { 1 };
    float x5[1] = { 1 };
    float x6[1] = { 1 };
    float x7[1] = { 1 };
    int x8[1] = { 0 };

    // ---------------- EQUIPMENT ----------------
    int eq_count = 1;
    int e_x1[1] = { 1 };
    int e_x2[1] = { 1 };
    float e_x3[1] = { 1 };
    float e_x4[1] = { 1 };
    float e_x5[1] = { 0 };
    int e_x6[1] = { 1 };  // MUST match labor
    float e_x7[1] = { 1 };
    float e_x8[1] = { 1 };
    float e_x9[1] = { 1 };
    int e_x10[1] = { 1 };

    // ---------------- PART ----------------
    int p_count = 1;
    int p_x1[1] = { 1 };
    float p_x2[1] = { 10 };   // demand
    float p_x3[1] = { 1 };    // lot size
    float p_x4[1] = { 1 };    // batch
    float p_x5[1] = { 1 };
    float p_x6[1] = { 1 };
    int p_x7[1] = { 0 };

    // ---------------- OPERATION ----------------
    int op_count = 1;
    int o_x1[1] = { 1 };
    int o_x2[1] = { 1 };
    int o_x3[1] = { 1 };  // part id
    int o_x4[1] = { 1 };  // eq id
    float o_x5[1] = { 1 };
    float o_x6[1] = { 1 };
    float o_x7[1] = { 1 };
    float o_x8[1] = { 1 };
    float o_x9[1] = { 1 };
    float o_x10[1] = { 1 };
    float o_x11[1] = { 1 };
    float o_x12[1] = { 1 };
    float o_x13[1] = { 1 };
    float o_x14[1] = { 1 };
    float o_x15[1] = { 1 };
    float o_x16[1] = { 1 };
    float o_x17[1] = { 1 };

    // ---------------- ROUTE ----------------
    int r_count = 1;
    int r_x1[1] = { 1 }; // part
    int r_x2[1] = { 1 }; // from op
    int r_x3[1] = { 1 }; // to op
    float r_x4[1] = { 1 };

    // ---------------- IBOM ----------------
    int ib_count = 0;
    int* i_x1 = nullptr;
    int* i_x2 = nullptr;
    float* i_x3 = nullptr;

    std::cout << "Running calculation..." << std::endl;

    irun_it(dir,
        1, 1, 1, 1, 1, 1,
        l_count, x1, x2, x3, x4, x5, x6, x7, x8,
        eq_count, e_x1, e_x2, e_x3, e_x4, e_x5, e_x6, e_x7, e_x8, e_x9, e_x10,
        p_count, p_x1, p_x2, p_x3, p_x4, p_x5, p_x6, p_x7,
        op_count, o_x1, o_x2, o_x3, o_x4, o_x5, o_x6, o_x7, o_x8, o_x9,
        o_x10, o_x11, o_x12, o_x13, o_x14, o_x15, o_x16, o_x17,
        r_count, r_x1, r_x2, r_x3, r_x4,
        ib_count, i_x1, i_x2, i_x3,
        1
    );

    std::cout << "Done" << std::endl;
}