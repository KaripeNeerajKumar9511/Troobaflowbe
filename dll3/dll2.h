// dll2.h : main header file for the dll2 DLL
//

#pragma once

#include <Windows.h>

#ifndef BEGIN_MESSAGE_MAP
#define BEGIN_MESSAGE_MAP(theClass, baseClass)
#define END_MESSAGE_MAP()
#define DECLARE_MESSAGE_MAP()
#endif

#ifndef _CWINAPP_STUB
#define _CWINAPP_STUB
#define EPSILON 1e-6
#define SSEPSILON 1e-20
class CWinApp {
public:
    CWinApp() {}
    virtual BOOL InitInstance() { return TRUE; }
};
#endif

// Cdll2App
// See dll2.cpp for the implementation of this class
//

class Cdll2App : public CWinApp
{
public:
	Cdll2App();

// Overrides
public:
	virtual BOOL InitInstance();

	DECLARE_MESSAGE_MAP()
};
