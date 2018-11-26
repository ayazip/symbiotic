//                     The LLVM Compiler Infrastructure
//
// This file is distributed under the University of Illinois Open Source
// License. See LICENSE.TXT for details.

#include <cassert>
#include <vector>
#include <set>
#include <fstream>
#include <sstream>

#include "llvm/IR/DataLayout.h"
#include "llvm/IR/BasicBlock.h"
#include "llvm/IR/Constants.h"
#include "llvm/IR/Function.h"
#include "llvm/IR/GlobalVariable.h"
#include "llvm/IR/Instructions.h"
#include "llvm/IR/Module.h"
#include "llvm/Pass.h"
#include "llvm/IR/Type.h"
#include "llvm/IR/TypeBuilder.h"
#if LLVM_VERSION_MAJOR >= 4 || (LLVM_VERSION_MAJOR == 3 && LLVM_VERSION_MINOR >= 5)
  #include "llvm/IR/InstIterator.h"
#else
  #include "llvm/Support/InstIterator.h"
#endif
#include "llvm/Support/raw_ostream.h"
#include "llvm/Transforms/Utils/BasicBlockUtils.h"

#include "llvm/Support/CommandLine.h"

using namespace llvm;

static cl::opt<std::string> source_name("replace-verifier-funs-source",
                                        cl::desc("Specify source filename"),
                                        cl::value_desc("filename"));


class ReplaceVerifierFuns : public ModulePass {
  // every item is (line number, call)
  std::vector<std::pair<unsigned, CallInst *>> calls_to_replace;
  std::vector<std::pair<unsigned, CallInst *>> allocs_to_handle;
  std::set<unsigned> lines_nums;
  std::map<unsigned, std::string> lines;
  Function *_vms = nullptr; // verifier_make_symbolic function
  Type *_size_t_Ty = nullptr; // type of size_t

  void handleCall(Function& F, CallInst *CI, bool ismalloc);
  void mapLines();
  void replaceCalls(Module& M);
  void handleAllocs(Module& M);
  void replaceCall(Module& M, CallInst *CI, unsigned line, const std::string& var);
  void handleAlloc(Module& M, CallInst *CI, unsigned line, const std::string& var);

  // add global of given type and initialize it in may as nondeterministic
  Function *get_verifier_make_nondet(llvm::Module&);
  Type *get_size_t(llvm::Module& );

  unsigned call_identifier = 0;

public:
  static char ID;

  ReplaceVerifierFuns() : ModulePass(ID) {}
  bool runOnFunction(Function &F);
  // must be module pass, so that we can iterate over
  // declarations too
  virtual bool runOnModule(Module &M) {
    for (auto& F : M)
      runOnFunction(F);

    mapLines();
    replaceCalls(M);
    handleAllocs(M);
    return !calls_to_replace.empty() || !allocs_to_handle.empty();
  }
};

bool ReplaceVerifierFuns::runOnFunction(Function &F) {
  if (!F.isDeclaration())
    return false;

  StringRef name = F.getName();
  if (!name.startswith("__VERIFIER_nondet_") &&
      !name.startswith("malloc") &&
      !name.startswith("calloc"))
    return false;

  bool changed = false;

  //llvm::errs() << "Got __VERIFIER_fun: " << name << "\n";

  for (auto I = F.use_begin(), E = F.use_end(); I != E; ++I) {
#if ((LLVM_VERSION_MAJOR == 3) && (LLVM_VERSION_MINOR < 5))
    Value *use = *I;
#else
    Value *use = I->getUser();
#endif

    if (CallInst *CI = dyn_cast<CallInst>(use)) {
      handleCall(F, CI, !name.startswith("__VERIFIER"));
    }
  }

  return changed;
}

void ReplaceVerifierFuns::handleCall(Function& F, CallInst *CI, bool ismalloc) {
  const DebugLoc& Loc = CI->getDebugLoc();
  if (Loc) {
    if (ismalloc)
	    allocs_to_handle.emplace_back(Loc.getLine(), CI);
    else
	    calls_to_replace.emplace_back(Loc.getLine(), CI);
    lines_nums.insert(Loc.getLine());
  } else {
    if (ismalloc)
	    allocs_to_handle.emplace_back(0, CI);
    else
	    calls_to_replace.emplace_back(0, CI);
  }
}

void ReplaceVerifierFuns::mapLines() {
  if (lines_nums.empty()) {
    assert(calls_to_replace.empty());
    return;
  }

  std::ifstream file(source_name);

  if (file.is_open()) {
    unsigned n = 1;
    std::string line;

    while (getline(file,line)) {
      if (lines_nums.count(n) > 0)
		lines[n] = std::move(line);
      ++n;
    }

    file.close();
  } else {
	errs() << "Couldn't open file: " << source_name << "\n";
    abort();
  }

  assert(lines.size() == lines_nums.size());
}

void ReplaceVerifierFuns::replaceCall(Module& M, CallInst *CI,
                                      unsigned line, const std::string& var) {
  std::string parent_name = cast<Function>(CI->getParent()->getParent())->getName();
  std::string name = parent_name + ":" + var + ":" + std::to_string(line);
  Constant *name_const = ConstantDataArray::getString(M.getContext(), name);
  GlobalVariable *nameG = new GlobalVariable(M, name_const->getType(), true /*constant */,
                                             GlobalVariable::PrivateLinkage, name_const);

  AllocaInst *AI = new AllocaInst(CI->getType()
#if (LLVM_VERSION_MAJOR >= 5)
  ,1
#endif
  );
  CastInst *CastI = CastInst::CreatePointerCast(AI, Type::getInt8PtrTy(M.getContext()));

  std::vector<Value *> args;
  // memory
  args.push_back(CastI);
  // nbytes
  args.push_back(ConstantInt::get(get_size_t(M),
                                  M.getDataLayout().getTypeAllocSize(CI->getType())));
  // name
  args.push_back(ConstantExpr::getPointerCast(nameG,
                                              Type::getInt8PtrTy(M.getContext())));
  // identifier
  args.push_back(ConstantInt::get(Type::getInt32Ty(M.getContext()), ++call_identifier));

  CallInst *new_CI = CallInst::Create(get_verifier_make_nondet(M), args);

  SmallVector<std::pair<unsigned, MDNode *>, 8> metadata;
  CI->getAllMetadata(metadata);
  // copy the metadata
  for (auto& md : metadata)
    new_CI->setMetadata(md.first, md.second);
  // copy the attributes (like zeroext etc.)
  new_CI->setAttributes(CI->getAttributes());


  LoadInst *LI = new LoadInst(AI, name);

  new_CI->insertBefore(CI);

  CastI->insertBefore(new_CI);
  AI->insertBefore(CastI);
  LI->insertAfter(new_CI);
  CI->replaceAllUsesWith(LI);
  CI->eraseFromParent();
}

void ReplaceVerifierFuns::handleAlloc(Module& M, CallInst *CI,
                                      unsigned line, const std::string& var) {
  static unsigned call_identifier = 0;
  std::string parent_name = cast<Function>(CI->getParent()->getParent())->getName();
  std::string name = parent_name + ":" + var + ":" + std::to_string(line);
  Constant *name_const = ConstantDataArray::getString(M.getContext(), name);
  GlobalVariable *nameG = new GlobalVariable(M, name_const->getType(), true /*constant */,
                                             GlobalVariable::PrivateLinkage, name_const);

  CastInst *CastI = CastInst::CreatePointerCast(CI, Type::getInt8PtrTy(M.getContext()));
  CastI->insertAfter(CI);

  std::vector<Value *> args;
  // memory
  args.push_back(CastI);
  // nbytes
  if (CI->getCalledFunction()->getName().equals("calloc")) {
    auto Mul = BinaryOperator::Create(Instruction::Mul,
                                      CI->getOperand(0),
                                      CI->getOperand(1));
    Mul->insertBefore(CastI);
    args.push_back(Mul);
  } else {
    args.push_back(CI->getOperand(0));
  }

  // name
  args.push_back(ConstantExpr::getPointerCast(nameG,
                                              Type::getInt8PtrTy(M.getContext())));
  // identifier
  args.push_back(ConstantInt::get(Type::getInt32Ty(M.getContext()), ++call_identifier));

  CallInst *new_CI = CallInst::Create(get_verifier_make_nondet(M), args);
  new_CI->insertAfter(CastI);
}

static std::string getName(const std::string& line) {
  std::istringstream iss(line);
  std::string sub, var;
  while (iss >> sub) {
    if (sub == "=") {
	  break;
    }
	var = std::move(sub);
  }

  if (!var.empty() && sub == "=") {
    // check also that after = follows the __VERIFIER_* call
    iss >> sub;
    // this may make problems with casting, line: (int) __VERIFIER_nondet_char()
    // maybe this is not needed?
    if (sub.compare(0, 18, "__VERIFIER_nondet_") == 0)
		return var;
  }

  return "--";
}

void ReplaceVerifierFuns::replaceCalls(Module& M) {
  for (auto& pr : calls_to_replace) {
    unsigned line_num = pr.first;
	CallInst *CI = pr.second;

    auto it = lines.find(line_num);
    replaceCall(M, CI, line_num,
                it == lines.end() ? "" : getName(it->second));
  }
}

void ReplaceVerifierFuns::handleAllocs(Module& M) {
  for (auto& pr : allocs_to_handle) {
    unsigned line_num = pr.first;
	CallInst *CI = pr.second;

    auto it = lines.find(line_num);
    handleAlloc(M, CI, line_num, "dynalloc");
  }
}

Function *ReplaceVerifierFuns::get_verifier_make_nondet(llvm::Module& M)
{
  if (_vms)
    return _vms;

  LLVMContext& Ctx = M.getContext();
  //void verifier_make_symbolic(void *addr, size_t nbytes, const char *name);
  Constant *C = M.getOrInsertFunction("klee_make_nondet",
                                      Type::getVoidTy(Ctx),
                                      Type::getInt8PtrTy(Ctx), // addr
                                      // FIXME: get rid of the nbytes
                                      // -- make the object symbolic entirely
                                      get_size_t(M),   // nbytes
                                      Type::getInt8PtrTy(Ctx), // name
                                      Type::getInt32Ty(Ctx), // identifier
                                      nullptr);
  _vms = cast<Function>(C);
  return _vms;
}

Type *ReplaceVerifierFuns::get_size_t(llvm::Module& M)
{
  if (_size_t_Ty)
    return _size_t_Ty;

  LLVMContext& Ctx = M.getContext();

  if (M.getDataLayout().getPointerSizeInBits() > 32)
    _size_t_Ty = Type::getInt64Ty(Ctx);
  else
    _size_t_Ty = Type::getInt32Ty(Ctx);

  return _size_t_Ty;
}

static RegisterPass<ReplaceVerifierFuns> RVF("replace-verifier-funs",
                                             "Replace calls to verifier funs with code "
                                             " that registers new symbolic objects "
                                             "with KLEE");
char ReplaceVerifierFuns::ID;

