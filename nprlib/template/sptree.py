import logging
from collections import defaultdict

from nprlib.task import TreeMerger
from nprlib.utils import GLOBALS, generate_runid, pjoin, rpath, DATATYPES

from nprlib.errors import DataError
from nprlib import db
from nprlib.template.common import (process_new_tasks, IterConfig,
                                    get_next_npr_node, get_iternumber)
from nprlib.logger import logindent

log = logging.getLogger("main")

def annotate_node(t, final_task):
    cladeid2node = {}
    # Annotate cladeid in the whole tree
    for n in t.traverse():
        if n.is_leaf():
            n.add_feature("realname", db.get_seq_name(n.name))
            #n.name = n.realname
        if hasattr(n, "cladeid"):
            cladeid2node[n.cladeid] = n

    alltasks = GLOBALS["nodeinfo"][final_task.nodeid]["tasks"]
    npr_iter = get_iternumber(final_task.threadid)
    n = cladeid2node[t.cladeid]
    n.add_features(size=final_task.size)
    for task in alltasks:
        params = ["%s %s" %(k,v) for k,v in  task.args.iteritems() 
                  if not k.startswith("_")]
        params = " ".join(params)

        if task.ttype == "tree":
            n.add_features(tree_model=task.model, 
                           tree_seqtype=task.seqtype, 
                           tree_type=task.tname, 
                           tree_cmd=params,
                           tree_file=rpath(task.tree_file),
                           tree_constrain=task.constrain_tree,
                           npr_iter=npr_iter)
            
        elif task.ttype == "treemerger":
            n.add_features(treemerger_type=task.tname, 
                           treemerger_rf="RF=%s [%s]" %(task.rf[0], task.rf[1]),
                           treemerger_out_match_dist = task.outgroup_match_dist,
                           treemerger_out_match = task.outgroup_match,)

        elif task.ttype == "concat_alg":
            n.add_features(concatalg_cogs="%d"%task.used_cogs,
                           )                       

def process_task(task, npr_conf, nodeid2info):
    cogconf, cogclass = npr_conf.cog_selector
    concatconf, concatclass = npr_conf.alg_concatenator
    treebuilderconf, treebuilderclass = npr_conf.tree_builder
    splitterconf, splitterclass = npr_conf.tree_splitter
    
    threadid, nodeid, seqtype, ttype = (task.threadid, task.nodeid,
                                        task.seqtype, task.ttype)
    cladeid, targets, outgroups = db.get_node_info(threadid, nodeid)
    if outgroups and len(outgroups) > 1:
        constrain_id = nodeid
    else:
        constrain_id = None
        
    node_info = nodeid2info[nodeid]
    conf = GLOBALS[task.configid]
    new_tasks = []    
    if ttype == "cog_selector":
        # register concat alignment task. NodeId associated to
        # concat_alg tasks and all its sibling jobs should take into
        # account cog information and not only species and outgroups
        # included.
        concat_job = concatclass(task.cogs,
                                 seqtype, conf, concatconf)
        db.add_node(threadid,
                    concat_job.nodeid, cladeid,
                    targets, outgroups)

        # Register Tree constrains
        constrain_tree = "(%s, (%s));" %(','.join(sorted(outgroups)), 
                                         ','.join(sorted(targets)))
        _outs = "\n".join(map(lambda name: ">%s\n0" %name, sorted(outgroups)))
        _tars = "\n".join(map(lambda name: ">%s\n1" %name, sorted(targets)))
        constrain_alg = '\n'.join([_outs, _tars])
        db.add_task_data(nodeid, DATATYPES.constrain_tree, constrain_tree)
        db.add_task_data(nodeid, DATATYPES.constrain_alg, constrain_alg)
        db.dataconn.commit() # since the creation of some Task objects
                             # may require this info, I need to commit
                             # right now.
        concat_job.size = task.size
        new_tasks.append(concat_job)
       
    elif ttype == "concat_alg":
        # register tree for concat alignment, using constraint tree if
        # necessary
        tree_task = treebuilderclass(nodeid, task.alg_phylip_file,
                                     constrain_id, "JTT",
                                     seqtype, conf, treebuilderconf)
        tree_task.size = task.size
        new_tasks.append(tree_task)
        
    elif ttype == "tree":
        merger_task = splitterclass(nodeid, seqtype, task.tree_file, conf, splitterconf)
        merger_task.size = task.size
        new_tasks.append(merger_task)

    elif ttype == "treemerger":
        # Lets merge with main tree
        if not task.task_tree:
            task.finish()

        log.log(28, "Saving task tree...")
        annotate_node(task.task_tree, task) 
        db.update_node(nid=task.nodeid, runid=task.threadid,
                       newick=db.encode(task.task_tree))
        db.commit()

        # Add new nodes
        source_seqtype = "aa" if "aa" in GLOBALS["seqtypes"] else "nt"
        ttree, mtree = task.task_tree, task.main_tree
        log.log(28, "Processing tree: %s seqs, %s outgroups",
                len(targets), len(outgroups))
        for node, seqs, outs in get_next_npr_node(task.configid, ttree,
                                                  mtree, None, npr_conf):
            log.log(28, "Adding new node: %s seqs, %s outgroups",
                    len(seqs), len(outs))
            new_task_node = cogclass(seqs, outs,
                                     source_seqtype, conf, cogconf)
            new_tasks.append(new_task_node)
            db.add_node(threadid,
                        new_task_node.nodeid, new_task_node.cladeid,
                        new_task_node.targets,
                        new_task_node.outgroups)
        
    return new_tasks
     

def pipeline(task, conf=None):
    logindent(2)
    # Points to npr parameters according to task properties
    nodeid2info = GLOBALS["nodeinfo"]
    if not task:
        source_seqtype = "aa" if "aa" in GLOBALS["seqtypes"] else "nt"
        npr_conf = IterConfig(conf, "sptree",
                              len(GLOBALS["target_species"]),
                              source_seqtype)
        cogconf, cogclass = npr_conf.cog_selector
        initial_task = cogclass(GLOBALS["target_species"], set(),
                                source_seqtype, conf, cogconf)

        initial_task.main_tree = main_tree = None
        initial_task.threadid = generate_runid()
        initial_task.configid = initial_task.threadid
        # Register node 
        db.add_node(initial_task.threadid, initial_task.nodeid,
                    initial_task.cladeid, initial_task.targets,
                    initial_task.outgroups)
        
        new_tasks = [initial_task]
    else:
        conf = GLOBALS[task.configid]
        npr_conf = IterConfig(conf, "sptree", task.size, task.seqtype)
        new_tasks  = process_task(task, npr_conf, nodeid2info)

    process_new_tasks(task, new_tasks)
    logindent(-2)
    
    return new_tasks
    
